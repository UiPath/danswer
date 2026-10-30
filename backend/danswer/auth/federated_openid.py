"""OIDC login client that authenticates with a federated credential instead of
a client secret.

httpx-oauth's OpenID client sends `client_secret` on the two token-endpoint
calls (authorization-code redemption and refresh). This subclass sends the
pod's AKS workload-identity token as a JWT client assertion instead; the app
registration has a federated identity credential that trusts that token's
issuer/subject, so Entra accepts it in place of a secret. Nothing secret is
stored anywhere: the token is projected by the kubelet and rotates on its own.
"""
import os
from typing import Any
from typing import cast
from typing import Dict
from typing import Optional

from httpx_oauth.clients.openid import OpenID
from httpx_oauth.oauth2 import GetAccessTokenError
from httpx_oauth.oauth2 import OAuth2Token
from httpx_oauth.oauth2 import RefreshTokenError
from httpx_oauth.oauth2 import RefreshTokenNotSupportedError

_CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


def _client_assertion() -> dict[str, str]:
    token_file = os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
    if not token_file:
        raise RuntimeError(
            "OAUTH_USE_WORKLOAD_IDENTITY is enabled but AZURE_FEDERATED_TOKEN_FILE "
            'is unset: the pod needs the `azure.workload.identity/use: "true"` '
            "label and a workload-identity ServiceAccount."
        )
    # Re-read every time: the kubelet rotates the projected token.
    with open(token_file) as f:
        assertion = f.read().strip()
    return {
        "client_assertion_type": _CLIENT_ASSERTION_TYPE,
        "client_assertion": assertion,
    }


class FederatedOpenID(OpenID):
    def __init__(self, client_id: str, openid_configuration_endpoint: str):
        # No secret: every token call carries a client assertion instead.
        super().__init__(client_id, "", openid_configuration_endpoint)

    async def get_access_token(
        self, code: str, redirect_uri: str, code_verifier: Optional[str] = None
    ) -> OAuth2Token:
        async with self.get_httpx_client() as client:
            data = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self.client_id,
                **_client_assertion(),
            }
            if code_verifier:
                data.update({"code_verifier": code_verifier})

            response = await client.post(
                self.access_token_endpoint,
                data=data,
                headers=self.request_headers,
            )
            body = cast(Dict[str, Any], response.json())
            if response.status_code >= 400:
                raise GetAccessTokenError(body)
            return OAuth2Token(body)

    async def refresh_token(self, refresh_token: str) -> OAuth2Token:
        if self.refresh_token_endpoint is None:
            raise RefreshTokenNotSupportedError()

        async with self.get_httpx_client() as client:
            response = await client.post(
                self.refresh_token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self.client_id,
                    **_client_assertion(),
                },
                headers=self.request_headers,
            )
            body = cast(Dict[str, Any], response.json())
            if response.status_code >= 400:
                raise RefreshTokenError(body)
            return OAuth2Token(body)
