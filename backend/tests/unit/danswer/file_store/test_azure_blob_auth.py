"""Unit tests for how the Azure Blob file store authenticates.

1. **Client selection**: a connection string wins (account key / Azurite);
   otherwise AZURE_BLOB_ACCOUNT_URL + the managed-identity credential; neither
   is a loud error.
2. **Upload SAS without an account key** is signed with a user-delegation key
   and keeps the one-blob, write+create, short-lived shape.
3. **Direct upload flag**: when off, the upload-url endpoint tells the client
   to use the server upload path, and never mints a SAS.
"""
from unittest import mock

import pytest

from danswer.db.entra_auth import WorkloadIdentityCredential
from danswer.file_store import file_store as fs
from danswer.server.query_and_chat import chat_backend


def test_connection_string_wins(monkeypatch) -> None:  # type: ignore
    monkeypatch.setattr(
        fs, "AZURE_BLOB_CONNECTION_STRING", "UseDevelopmentStorage=true"
    )
    monkeypatch.setattr(
        fs, "AZURE_BLOB_ACCOUNT_URL", "https://acct.blob.core.windows.net"
    )
    with mock.patch("azure.storage.blob.BlobServiceClient") as bsc:
        fs._get_azure_blob_service_client()
    bsc.from_connection_string.assert_called_once_with("UseDevelopmentStorage=true")
    bsc.assert_not_called()


def test_account_url_uses_managed_identity(monkeypatch) -> None:  # type: ignore
    monkeypatch.setattr(fs, "AZURE_BLOB_CONNECTION_STRING", "")
    monkeypatch.setattr(
        fs, "AZURE_BLOB_ACCOUNT_URL", "https://acct.blob.core.windows.net"
    )
    with mock.patch("azure.storage.blob.BlobServiceClient") as bsc:
        fs._get_azure_blob_service_client()
    args, kwargs = bsc.call_args
    assert args == ("https://acct.blob.core.windows.net",)
    assert isinstance(kwargs["credential"], WorkloadIdentityCredential)
    bsc.from_connection_string.assert_not_called()


def test_neither_configured_is_an_error(monkeypatch) -> None:  # type: ignore
    monkeypatch.setattr(fs, "AZURE_BLOB_CONNECTION_STRING", "")
    monkeypatch.setattr(fs, "AZURE_BLOB_ACCOUNT_URL", "")
    with pytest.raises(RuntimeError, match="AZURE_BLOB_ACCOUNT_URL"):
        fs._get_azure_blob_service_client()


def test_upload_sas_uses_user_delegation_key(monkeypatch) -> None:  # type: ignore
    monkeypatch.setattr(fs, "AZURE_BLOB_CONNECTION_STRING", "")
    svc = mock.MagicMock(account_name="acct", url="https://acct.blob.core.windows.net/")
    svc.get_user_delegation_key.return_value = "udk"
    monkeypatch.setattr(fs, "_get_azure_blob_service_client", lambda: svc)
    with mock.patch(
        "azure.storage.blob.generate_blob_sas", return_value="sig=abc"
    ) as gen:
        url = fs.AzureBlobFileStore(mock.MagicMock()).generate_upload_sas_url(
            "file-1", expiry_minutes=30
        )
    assert (
        url
        == f"https://acct.blob.core.windows.net/{fs.AZURE_BLOB_CONTAINER}/file-1?sig=abc"
    )
    kw = gen.call_args.kwargs
    assert kw["user_delegation_key"] == "udk"
    assert "account_key" not in kw
    assert kw["blob_name"] == "file-1"
    perm = kw["permission"]
    assert perm.write and perm.create and not perm.read and not perm.delete
    assert (kw["expiry"] - kw["start"]).total_seconds() <= 36 * 60


def test_direct_upload_disabled_falls_back_to_server(monkeypatch) -> None:  # type: ignore
    monkeypatch.setattr(chat_backend, "FILE_STORE_TYPE", "AzureBlobFileStore")
    monkeypatch.setattr(chat_backend, "AZURE_BLOB_DIRECT_UPLOAD_ENABLED", False)
    with mock.patch.object(chat_backend, "get_default_file_store") as get_store:
        resp = chat_backend.get_chat_file_upload_urls(
            chat_backend.ChatFileUploadUrlRequest(files=[]),
            db_session=mock.MagicMock(),
            _=None,
        )
    assert resp.direct_upload is False
    get_store.assert_not_called()
