import threading
from abc import ABC
from abc import abstractmethod
from io import BytesIO
from tempfile import SpooledTemporaryFile
from typing import Any
from typing import IO

from sqlalchemy.orm import Session

from danswer.configs.app_configs import AZURE_BLOB_CONNECTION_STRING
from danswer.configs.app_configs import AZURE_BLOB_CONTAINER
from danswer.configs.app_configs import FILE_STORE_TYPE
from danswer.configs.constants import FileOrigin
from danswer.db.models import PGFileStore
from danswer.db.pg_file_store import create_populate_lobj
from danswer.db.pg_file_store import delete_lobj_by_id
from danswer.db.pg_file_store import delete_pgfilestore_by_file_name
from danswer.db.pg_file_store import get_pgfilestore_by_file_name
from danswer.db.pg_file_store import read_lobj
from danswer.db.pg_file_store import upsert_pgfilestore
from danswer.file_store.constants import MAX_IN_MEMORY_SIZE
from danswer.utils.logger import setup_logger

logger = setup_logger()


class FileStore(ABC):
    """
    An abstraction for storing files and large binary objects.
    """

    @abstractmethod
    def save_file(
        self,
        file_name: str,
        content: IO,
        display_name: str | None,
        file_origin: FileOrigin,
        file_type: str,
        file_metadata: dict | None = None,
    ) -> None:
        """
        Save a file to the blob store

        Parameters:
        - connector_name: Name of the CC-Pair (as specified by the user in the UI)
        - file_name: Name of the file to save
        - content: Contents of the file
        - display_name: Display name of the file
        - file_origin: Origin of the file
        - file_type: Type of the file
        """
        raise NotImplementedError

    @abstractmethod
    def read_file(
        self, file_name: str, mode: str | None, use_tempfile: bool = False
    ) -> IO:
        """
        Read the content of a given file by the name

        Parameters:
        - file_name: Name of file to read
        - mode: Mode to open the file (e.g. 'b' for binary)
        - use_tempfile: Whether to use a temporary file to store the contents
                        in order to avoid loading the entire file into memory

        Returns:
            Contents of the file and metadata dict
        """

    @abstractmethod
    def delete_file(self, file_name: str) -> None:
        """
        Delete a file by its name.

        Parameters:
        - file_name: Name of file to delete
        """


class PostgresBackedFileStore(FileStore):
    def __init__(self, db_session: Session):
        self.db_session = db_session

    def save_file(
        self,
        file_name: str,
        content: IO,
        display_name: str | None,
        file_origin: FileOrigin,
        file_type: str,
        file_metadata: dict | None = None,
    ) -> None:
        try:
            # The large objects in postgres are saved as special objects can be listed with
            # SELECT * FROM pg_largeobject_metadata;
            obj_id = create_populate_lobj(content=content, db_session=self.db_session)
            upsert_pgfilestore(
                file_name=file_name,
                display_name=display_name or file_name,
                file_origin=file_origin,
                file_type=file_type,
                lobj_oid=obj_id,
                db_session=self.db_session,
                file_metadata=file_metadata,
            )
            self.db_session.commit()
        except Exception:
            self.db_session.rollback()
            raise

    def read_file(
        self, file_name: str, mode: str | None = None, use_tempfile: bool = False
    ) -> IO:
        file_record = get_pgfilestore_by_file_name(
            file_name=file_name, db_session=self.db_session
        )
        if file_record.lobj_oid is None:
            # Row was written by an object-store backend — can't read it as a
            # large object. Indicates FILE_STORE_TYPE was changed without
            # migrating, or the wrong backend is active.
            raise RuntimeError(
                f"File '{file_name}' has no Postgres large object "
                f"(object_key={file_record.object_key!r}); is FILE_STORE_TYPE correct?"
            )
        return read_lobj(
            lobj_oid=file_record.lobj_oid,
            db_session=self.db_session,
            mode=mode,
            use_tempfile=use_tempfile,
        )

    def read_file_record(self, file_name: str) -> PGFileStore:
        file_record = get_pgfilestore_by_file_name(
            file_name=file_name, db_session=self.db_session
        )

        return file_record

    def delete_file(self, file_name: str) -> None:
        try:
            file_record = get_pgfilestore_by_file_name(
                file_name=file_name, db_session=self.db_session
            )
            if file_record.lobj_oid is not None:
                delete_lobj_by_id(file_record.lobj_oid, db_session=self.db_session)
            delete_pgfilestore_by_file_name(
                file_name=file_name, db_session=self.db_session
            )
            self.db_session.commit()
        except Exception:
            self.db_session.rollback()
            raise


# --- Azure Blob backend -----------------------------------------------------
# The azure SDK is an OPTIONAL dependency: file_store.py is imported app-wide,
# so we must NOT import azure at module load. It's lazily imported only when
# the Azure backend is actually constructed (and the package is present in the
# image). The container client is a process-wide lazy singleton.
_az_container_client: Any = None
_az_lock = threading.Lock()


def _get_azure_container_client() -> Any:
    global _az_container_client
    if _az_container_client is None:
        with _az_lock:
            if _az_container_client is None:
                if not AZURE_BLOB_CONNECTION_STRING:
                    raise RuntimeError(
                        "FILE_STORE_TYPE=AzureBlobFileStore but "
                        "AZURE_BLOB_CONNECTION_STRING is unset."
                    )
                # Lazy import — optional dependency (azure-storage-blob).
                from azure.storage.blob import BlobServiceClient  # type: ignore

                svc = BlobServiceClient.from_connection_string(
                    AZURE_BLOB_CONNECTION_STRING
                )
                cc = svc.get_container_client(AZURE_BLOB_CONTAINER)
                try:
                    cc.create_container()
                except Exception:
                    # Already exists (or no create permission) — fine.
                    pass
                _az_container_client = cc
    return _az_container_client


class AzureBlobFileStore(FileStore):
    """File store that keeps the BYTES in Azure Blob Storage and the METADATA
    row in Postgres (``file_store`` table, ``object_key`` column).

    Why hybrid: the metadata is small and queryable, but the blob bytes are
    what bloat Postgres and (via read_lobj) pin a DB connection for the whole
    read. Moving only the bytes off-DB fixes both — reads stream straight from
    Blob and never hold a Postgres connection.

    Reads fall back to the Postgres large object when a row hasn't been
    migrated yet (``object_key is None`` but ``lobj_oid`` set), so the cutover
    is graceful: flip FILE_STORE_TYPE, new files go to Blob, old files keep
    working until the migration script moves them.
    """

    def __init__(self, db_session: Session):
        self.db_session = db_session

    def save_file(
        self,
        file_name: str,
        content: IO,
        display_name: str | None,
        file_origin: FileOrigin,
        file_type: str,
        file_metadata: dict | None = None,
    ) -> None:
        object_key = file_name  # file_name is already the unique identifier
        try:
            # upload_blob streams `content` in chunks — no whole-file-in-memory.
            _get_azure_container_client().upload_blob(
                name=object_key, data=content, overwrite=True
            )
            upsert_pgfilestore(
                file_name=file_name,
                display_name=display_name or file_name,
                file_origin=file_origin,
                file_type=file_type,
                object_key=object_key,
                lobj_oid=None,
                db_session=self.db_session,
                file_metadata=file_metadata,
            )
            self.db_session.commit()
        except Exception:
            self.db_session.rollback()
            raise

    def read_file(
        self, file_name: str, mode: str | None = None, use_tempfile: bool = False
    ) -> IO:
        record = get_pgfilestore_by_file_name(
            file_name=file_name, db_session=self.db_session
        )
        if record.object_key is None:
            # Not yet migrated — read from the legacy Postgres large object.
            if record.lobj_oid is None:
                raise RuntimeError(
                    f"File '{file_name}' has neither object_key nor lobj_oid."
                )
            return read_lobj(
                lobj_oid=record.lobj_oid,
                db_session=self.db_session,
                mode=mode,
                use_tempfile=use_tempfile,
            )

        downloader = _get_azure_container_client().download_blob(record.object_key)
        if use_tempfile:
            temp_file: IO = SpooledTemporaryFile(max_size=MAX_IN_MEMORY_SIZE)
            downloader.readinto(temp_file)
            temp_file.seek(0)
            return temp_file
        return BytesIO(downloader.readall())

    def delete_file(self, file_name: str) -> None:
        try:
            record = get_pgfilestore_by_file_name(
                file_name=file_name, db_session=self.db_session
            )
            if record.object_key is not None:
                try:
                    _get_azure_container_client().delete_blob(record.object_key)
                except Exception:
                    logger.error(
                        f"Failed to delete blob {record.object_key}; "
                        "removing the metadata row anyway."
                    )
            elif record.lobj_oid is not None:
                delete_lobj_by_id(record.lobj_oid, db_session=self.db_session)
            delete_pgfilestore_by_file_name(
                file_name=file_name, db_session=self.db_session
            )
            self.db_session.commit()
        except Exception:
            self.db_session.rollback()
            raise


def get_default_file_store(db_session: Session) -> FileStore:
    """Resolve the configured file-store backend (FILE_STORE_TYPE).

    Default is Postgres large objects. AzureBlobFileStore offloads the bytes
    to Azure Blob Storage (metadata stays in Postgres) — opt-in per env.
    """
    if FILE_STORE_TYPE == AzureBlobFileStore.__name__:
        return AzureBlobFileStore(db_session=db_session)
    return PostgresBackedFileStore(db_session=db_session)
