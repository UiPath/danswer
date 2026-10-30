"""Unit tests for ``danswer.auth.federated_openid.FederatedOpenID``.

What we lock down: both token-endpoint calls (code redemption and refresh)
authenticate with the projected workload-identity token as a JWT client
assertion — re-read on every call, since the kubelet rotates it — and never
send a client_secret. Errors from Entra surface as the usual httpx-oauth
exceptions so fastapi-users handles them unchanged.
"""
import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest import mock

import pytest
from httpx_oauth.oauth2 import GetAccessTokenError
from httpx_oauth.oauth2 import RefreshTokenError

from danswer.auth import federated_openid
from danswer.auth.federated_openid import FederatedOpenID

_DISCOVERY = {
    "authorization_endpoint": "https://login.example/authorize",
    "token_endpoint": "https://login.example/token",
    "jwks_uri": "https://login.example/keys",
    "grant_types_supported": ["authorization_code", "refresh_token"],
}


class _FakeResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, data: dict, headers: dict) -> _FakeResponse:
        self.calls.append({"url": url, "data": data})
        return self.response


@pytest.fixture
def token_file(tmp_path, monkeypatch):  # type: ignore
    f = tmp_path / "azure-identity-token"
    f.write_text("sa-token-1\n")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(f))
    return f


def _client(response: _FakeResponse) -> tuple[FederatedOpenID, _FakeClient]:
    discovery = mock.MagicMock(status_code=200)
    discovery.json.return_value = _DISCOVERY
    http = mock.MagicMock()
    http.__enter__.return_value.get.return_value = discovery
    with mock.patch("httpx_oauth.clients.openid.httpx.Client", return_value=http):
        oidc = FederatedOpenID("app-id", "https://login.example/.well-known")
    fake = _FakeClient(response)

    @asynccontextmanager
    async def _httpx_client():  # type: ignore
        yield fake

    oidc.get_httpx_client = _httpx_client  # type: ignore
    return oidc, fake


def test_code_redemption_uses_assertion_not_secret(token_file) -> None:  # type: ignore
    oidc, fake = _client(_FakeResponse(200, {"access_token": "at", "id_token": "it"}))
    token = asyncio.run(oidc.get_access_token("the-code", "https://app/cb"))

    assert token["access_token"] == "at"
    call = fake.calls[0]
    assert call["url"] == "https://login.example/token"
    assert call["data"]["grant_type"] == "authorization_code"
    assert call["data"]["code"] == "the-code"
    assert call["data"]["client_id"] == "app-id"
    assert call["data"]["client_assertion"] == "sa-token-1"
    assert (
        call["data"]["client_assertion_type"]
        == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    )
    assert "client_secret" not in call["data"]


def test_refresh_uses_fresh_assertion(token_file) -> None:  # type: ignore
    oidc, fake = _client(_FakeResponse(200, {"access_token": "at2"}))
    token_file.write_text("sa-token-2\n")  # kubelet rotated the token
    asyncio.run(oidc.refresh_token("rt"))

    data = fake.calls[0]["data"]
    assert data["grant_type"] == "refresh_token"
    assert data["client_assertion"] == "sa-token-2"
    assert "client_secret" not in data


def test_entra_errors_surface_as_httpx_oauth_errors(token_file) -> None:  # type: ignore
    err = _FakeResponse(400, {"error": "invalid_client"})
    oidc, _ = _client(err)
    with pytest.raises(GetAccessTokenError):
        asyncio.run(oidc.get_access_token("c", "https://app/cb"))
    with pytest.raises(RefreshTokenError):
        asyncio.run(oidc.refresh_token("rt"))


def test_missing_workload_identity_fails_loudly(monkeypatch) -> None:  # type: ignore
    monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
    with pytest.raises(RuntimeError, match="AZURE_FEDERATED_TOKEN_FILE"):
        federated_openid._client_assertion()
