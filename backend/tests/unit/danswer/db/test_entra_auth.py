"""Unit tests for passwordless Postgres login (``danswer.db.entra_auth`` and
``danswer.db.engine.attach_entra_auth``).

What we lock down here:

1. **Token exchange** uses the projected workload-identity token as the client
   assertion (re-read on every acquisition, since the kubelet rotates it) and
   asks for the Postgres scope.
2. **Caching**: a token is reused until it is within the refresh margin of
   expiry, then re-acquired.
3. **Fail loudly** when workload identity isn't wired into the pod, or Entra
   refuses the exchange — never fall back to a password silently.
4. **Per-scope cache**: a Postgres token is never handed to Blob (or v.v.),
   and the azure-core credential returns a proper AccessToken.
5. **Engine hook**: with the flag on, every new DBAPI connection gets the
   token as its password and TLS; with the flag off, nothing is attached.
"""
from unittest import mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine

from danswer.db import engine as engine_mod
from danswer.db import entra_auth


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    entra_auth._cache.clear()


@pytest.fixture
def wi_env(tmp_path, monkeypatch):  # type: ignore
    token_file = tmp_path / "azure-identity-token"
    token_file.write_text("sa-token-1\n")
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant-id")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com/")
    return token_file


def _fake_msal(expires_in: int = 3600, tokens: list[str] | None = None):  # type: ignore
    tokens = list(tokens or ["entra-token-1", "entra-token-2"])
    app = mock.MagicMock()
    app.acquire_token_for_client.side_effect = lambda scopes: {
        "access_token": tokens.pop(0),
        "expires_in": expires_in,
    }
    return mock.patch.object(
        entra_auth.msal, "ConfidentialClientApplication", return_value=app
    )


def test_exchanges_projected_token_for_postgres_scope(wi_env) -> None:  # type: ignore
    with _fake_msal() as cca:
        assert entra_auth.get_postgres_entra_token() == "entra-token-1"

    args, kwargs = cca.call_args
    assert args[0] == "client-id"
    assert kwargs["authority"] == "https://login.microsoftonline.com/tenant-id"
    assert kwargs["client_credential"] == {"client_assertion": "sa-token-1"}
    cca.return_value.acquire_token_for_client.assert_called_once_with(
        scopes=["https://ossrdbms-aad.database.windows.net/.default"]
    )


def test_reuses_token_until_refresh_margin(wi_env) -> None:  # type: ignore
    with _fake_msal(expires_in=3600) as cca, mock.patch.object(
        entra_auth.time, "time", return_value=1000.0
    ) as now:
        assert entra_auth.get_postgres_entra_token() == "entra-token-1"
        now.return_value = 1000.0 + 3600 - 301  # still outside the 300s margin
        assert entra_auth.get_postgres_entra_token() == "entra-token-1"
        assert cca.call_count == 1

        wi_env.write_text("sa-token-2\n")  # kubelet rotated the projected token
        now.return_value = 1000.0 + 3600 - 299  # inside the margin -> refresh
        assert entra_auth.get_postgres_entra_token() == "entra-token-2"
        assert cca.call_count == 2
        assert cca.call_args.kwargs["client_credential"] == {
            "client_assertion": "sa-token-2"
        }


def test_missing_workload_identity_env_fails_loudly(monkeypatch) -> None:  # type: ignore
    for var in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_FEDERATED_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="workload identity is not wired"):
        entra_auth.get_postgres_entra_token()


def test_entra_error_is_surfaced(wi_env) -> None:  # type: ignore
    app = mock.MagicMock()
    app.acquire_token_for_client.return_value = {
        "error": "invalid_client",
        "error_description": "AADSTS70021: No matching federated identity record",
    }
    with mock.patch.object(
        entra_auth.msal, "ConfidentialClientApplication", return_value=app
    ), pytest.raises(RuntimeError, match="AADSTS70021"):
        entra_auth.get_postgres_entra_token()


def _fire_do_connect(sync_engine, driver: str) -> dict:  # type: ignore
    """Run the engine's do_connect listeners the way SQLAlchemy would."""
    cparams = {"password": "from-url", "host": "db"}
    dialect = mock.MagicMock(driver=driver)
    sync_engine.dialect.dispatch.do_connect(dialect, mock.MagicMock(), [], cparams)
    return cparams


def test_attach_sets_token_and_tls_on_sync_engine(monkeypatch) -> None:  # type: ignore
    monkeypatch.setattr(engine_mod, "POSTGRES_ENTRA_AUTH", True)
    eng = create_engine("postgresql+psycopg2://u:from-url@db/x")
    with mock.patch.object(entra_auth, "get_postgres_entra_token", return_value="tok"):
        engine_mod.attach_entra_auth(eng)
        cparams = _fire_do_connect(eng, "psycopg2")
    assert cparams["password"] == "tok"
    assert cparams["sslmode"] == "require"


def test_attach_sets_token_and_tls_on_async_engine(monkeypatch) -> None:  # type: ignore
    monkeypatch.setattr(engine_mod, "POSTGRES_ENTRA_AUTH", True)
    eng = create_async_engine("postgresql+asyncpg://u:from-url@db/x")
    with mock.patch.object(entra_auth, "get_postgres_entra_token", return_value="tok"):
        engine_mod.attach_entra_auth(eng)
        cparams = _fire_do_connect(eng.sync_engine, "asyncpg")
    assert cparams["password"] == "tok"
    assert cparams["ssl"] == "require"


def test_attach_is_noop_when_flag_off(monkeypatch) -> None:  # type: ignore
    monkeypatch.setattr(engine_mod, "POSTGRES_ENTRA_AUTH", False)
    eng = create_engine("postgresql+psycopg2://u:p@db/x")
    engine_mod.attach_entra_auth(eng)
    assert len(eng.dialect.dispatch.do_connect) == 0


def test_tokens_are_cached_per_scope(wi_env) -> None:  # type: ignore
    with _fake_msal(tokens=["pg-token", "blob-token"]) as cca:
        assert entra_auth.get_postgres_entra_token() == "pg-token"
        cred = entra_auth.WorkloadIdentityCredential()
        tok = cred.get_token("https://storage.azure.com/.default")
        assert tok.token == "blob-token"
        assert tok.expires_on > 0
        # both cached now: no further exchanges
        assert entra_auth.get_postgres_entra_token() == "pg-token"
        assert (
            cred.get_token("https://storage.azure.com/.default").token == "blob-token"
        )
        assert cca.call_count == 2
    scopes = [
        c.kwargs["scopes"]
        for c in cca.return_value.acquire_token_for_client.call_args_list
    ]
    assert scopes == [
        ["https://ossrdbms-aad.database.windows.net/.default"],
        ["https://storage.azure.com/.default"],
    ]


def test_credential_rejects_multiple_scopes() -> None:
    with pytest.raises(ValueError):
        entra_auth.WorkloadIdentityCredential().get_token("a", "b")
