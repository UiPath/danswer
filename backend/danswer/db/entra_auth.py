"""Passwordless access to Azure services via Microsoft Entra workload identity.

Tokens are obtained for the pod's managed identity through AKS workload
identity: the webhook projects a Kubernetes service-account token at
AZURE_FEDERATED_TOKEN_FILE, which is exchanged (client-assertion flow) for an
Entra access token for the requested scope. Used for:

- Postgres (POSTGRES_ENTRA_AUTH): every NEW connection authenticates with a
  short-lived token as its password. Postgres only validates the token at
  connect time, so pooled connections keep working after it expires.
- Azure Blob Storage (AZURE_BLOB_ACCOUNT_URL): WorkloadIdentityCredential is
  handed to the storage SDK in place of an account key.
"""
import os
import threading
import time
from typing import Any

import msal  # type: ignore

from danswer.utils.logger import setup_logger

logger = setup_logger()

_POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
# Refresh when less than this much lifetime is left, so a request never races
# the expiry.
_REFRESH_MARGIN_SECONDS = 300

_lock = threading.Lock()
# scope -> (access token, expires_at epoch seconds)
_cache: dict[str, tuple[str, float]] = {}


def _acquire_token(scope: str) -> tuple[str, float]:
    client_id = os.environ.get("AZURE_CLIENT_ID")
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    token_file = os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
    authority_host = (
        os.environ.get("AZURE_AUTHORITY_HOST") or "https://login.microsoftonline.com/"
    )
    if not (client_id and tenant_id and token_file):
        raise RuntimeError(
            "Entra (managed identity) auth is enabled but workload identity is not "
            "wired into this pod (AZURE_CLIENT_ID / AZURE_TENANT_ID / "
            "AZURE_FEDERATED_TOKEN_FILE unset). The pod needs the "
            '`azure.workload.identity/use: "true"` label and a ServiceAccount '
            "annotated with the managed identity's client id."
        )

    # Re-read every time: the kubelet rotates the projected token.
    with open(token_file) as f:
        assertion = f.read().strip()

    app = msal.ConfidentialClientApplication(
        client_id,
        authority=f"{authority_host.rstrip('/')}/{tenant_id}",
        client_credential={"client_assertion": assertion},
    )
    result = app.acquire_token_for_client(scopes=[scope])
    if "access_token" not in result:
        raise RuntimeError(
            f"Failed to get an Entra token for {scope}: "
            f"{result.get('error')}: {result.get('error_description')}"
        )
    return result["access_token"], time.time() + int(result.get("expires_in", 3600))


def get_entra_token_with_expiry(scope: str) -> tuple[str, float]:
    """Return (token, expires_at) for `scope`, refreshing when near expiry."""
    with _lock:
        cached = _cache.get(scope)
        if cached is None or time.time() > cached[1] - _REFRESH_MARGIN_SECONDS:
            cached = _acquire_token(scope)
            _cache[scope] = cached
            logger.info(f"Acquired Entra token for {scope} (managed identity)")
        return cached


def get_postgres_entra_token() -> str:
    """Return a valid Entra access token for Postgres."""
    return get_entra_token_with_expiry(_POSTGRES_SCOPE)[0]


class WorkloadIdentityCredential:
    """Minimal azure-core TokenCredential backed by the cache above, so Azure
    SDK clients (e.g. BlobServiceClient) can run on the pod's managed identity
    without pulling in azure-identity."""

    def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        from azure.core.credentials import AccessToken  # type: ignore

        if len(scopes) != 1:
            raise ValueError(f"Expected exactly one scope, got {scopes!r}")
        token, expires_at = get_entra_token_with_expiry(scopes[0])
        return AccessToken(token, int(expires_at))
