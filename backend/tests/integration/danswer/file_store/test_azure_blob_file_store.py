"""Integration test for the Azure Blob file store — big-file round-trip.

Verifies the real AzureBlobFileStore end-to-end against a live Blob endpoint
(Azurite emulator locally, or a real storage account): a LARGE file is
streamed up, streamed back down, and its bytes must match — exercising the
streaming/spool paths that the OOM crash exposed. Metadata lives in the
Postgres `file_store` table, so a reachable + migrated DB is also required.

This test is SKIPPED unless AZURE_BLOB_CONNECTION_STRING is set, so it never
runs (or breaks) in environments without Blob configured.

Run it (locally, against Azurite):

    # 1. Start Azurite (Azure Storage emulator):
    docker run -d -p 10000:10000 mcr.microsoft.com/azure-storage/azurite \
        azurite-blob --blobHost 0.0.0.0
    # 2. Install the optional dep into your venv:
    pip install azure-storage-blob==12.19.1
    # 3. Point the test at Azurite (well-known dev connection string) + your
    #    local Postgres (must have run `alembic upgrade head` for object_key):
    export AZURE_BLOB_CONNECTION_STRING="DefaultEndpointsProtocol=http;\
AccountName=devstoreaccount1;\
AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;\
BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
    export AZURE_BLOB_CONTAINER=danswer-files-test
    # 4. Run:
    PYTHONPATH=$(pwd) pytest tests/integration/danswer/file_store/test_azure_blob_file_store.py -v
"""
import hashlib
import os
import uuid
from io import BytesIO

import pytest
from sqlalchemy.orm import Session

from danswer.configs.constants import FileOrigin
from danswer.db.engine import get_sqlalchemy_engine

pytestmark = pytest.mark.skipif(
    not os.environ.get("AZURE_BLOB_CONNECTION_STRING"),
    reason="AZURE_BLOB_CONNECTION_STRING unset — Azure Blob integration test skipped.",
)

# 40 MB — comfortably above MAX_IN_MEMORY_SIZE (30 MB), so the read path must
# spill to the spooled temp file rather than holding it all in memory.
BIG_SIZE = 40 * 1024 * 1024


def _sha256(stream) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        h.update(chunk)
    return h.hexdigest()


def test_azure_blob_big_file_round_trip() -> None:
    from danswer.file_store.file_store import AzureBlobFileStore

    file_name = f"integration-test/big-{uuid.uuid4()}.bin"
    content = os.urandom(BIG_SIZE)
    expected = hashlib.sha256(content).hexdigest()

    with Session(get_sqlalchemy_engine()) as db_session:
        store = AzureBlobFileStore(db_session=db_session)
        try:
            # --- streaming upload ---
            store.save_file(
                file_name=file_name,
                content=BytesIO(content),
                display_name="big upload integration test",
                file_origin=FileOrigin.OTHER,
                file_type="application/octet-stream",
            )

            # --- streaming download (use_tempfile=True → spools to disk) ---
            got = store.read_file(file_name, mode="b", use_tempfile=True)
            assert _sha256(got) == expected, "round-tripped bytes differ (streamed)"

            # --- in-memory download path too ---
            got2 = store.read_file(file_name, mode="b")
            assert got2.read() == content, "round-tripped bytes differ (in-memory)"

            # --- metadata row points at Blob, not a lobj ---
            from danswer.db.pg_file_store import get_pgfilestore_by_file_name

            record = get_pgfilestore_by_file_name(file_name, db_session)
            assert record.object_key is not None
            assert record.lobj_oid is None
        finally:
            # Always clean up the blob + metadata row.
            try:
                store.delete_file(file_name)
            except Exception:
                pass

        # --- deletion removed it ---
        with pytest.raises(Exception):
            store.read_file(file_name, mode="b")
