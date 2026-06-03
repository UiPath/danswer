"""Migrate file bytes from Postgres large objects → Azure Blob Storage.

For every `file_store` row that still has its bytes in a Postgres large
object (`lobj_oid` set, `object_key` NULL), this streams the lobj up to the
Azure Blob container, points the row at the blob (`object_key`), clears
`lobj_oid`, and frees the large object.

Idempotent — already-migrated rows (object_key set) are skipped, so it's
safe to re-run / resume. Reads use a spooled temp file, so a huge file
won't OOM the migrator.

Requires the Azure backend to be configured in the environment:
    FILE_STORE_TYPE=AzureBlobFileStore   (not strictly required, but matches prod)
    AZURE_BLOB_CONNECTION_STRING=...      (the storage account connection string)
    AZURE_BLOB_CONTAINER=danswer-files

Usage:
    cd backend
    PYTHONPATH=$(pwd) python scripts/migrate_file_store_to_azure_blob.py
    PYTHONPATH=$(pwd) python scripts/migrate_file_store_to_azure_blob.py --dry-run

Cutover: deploy the image (with azure-storage-blob) + the migration that
adds object_key, set the secret, flip FILE_STORE_TYPE=AzureBlobFileStore,
then run this once. Reads of un-migrated rows fall back to the lobj in the
meantime, so there's no hard ordering requirement — but run it promptly so
the lobjs (and the DB bloat) actually go away.
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from danswer.db.engine import get_sqlalchemy_engine
from danswer.db.models import PGFileStore
from danswer.db.pg_file_store import delete_lobj_by_id
from danswer.db.pg_file_store import read_lobj
from danswer.file_store.file_store import _get_azure_container_client


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would migrate; upload/modify nothing.",
    )
    args = parser.parse_args()

    engine = get_sqlalchemy_engine()
    with Session(engine) as db_session:
        rows = db_session.scalars(
            select(PGFileStore)
            .where(PGFileStore.lobj_oid.isnot(None))
            .where(PGFileStore.object_key.is_(None))
        ).all()
        print(f"{len(rows)} file(s) to migrate (lobj → blob).")
        if args.dry_run:
            for r in rows:
                print(f"  would migrate: {r.file_name} (lobj_oid={r.lobj_oid})")
            return 0

        container = _get_azure_container_client()
        migrated = 0
        for r in rows:
            old_lobj = r.lobj_oid
            # Spooled temp file → bounded memory even for large blobs.
            stream = read_lobj(
                lobj_oid=old_lobj, db_session=db_session, use_tempfile=True
            )
            container.upload_blob(name=r.file_name, data=stream, overwrite=True)

            # Point the row at the blob, then free the lobj. Commit the row
            # first so a crash leaves it readable from the blob (the lobj
            # delete is best-effort cleanup).
            r.object_key = r.file_name
            r.lobj_oid = None
            db_session.commit()
            try:
                delete_lobj_by_id(old_lobj, db_session=db_session)
                db_session.commit()
            except Exception as e:
                print(
                    f"  WARN: uploaded {r.file_name} but failed to free lobj {old_lobj}: {e}"
                )
                db_session.rollback()

            migrated += 1
            if migrated % 50 == 0:
                print(f"  migrated {migrated}/{len(rows)}…")

        print(f"Done. Migrated {migrated} file(s) to Azure Blob.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
