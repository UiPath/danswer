from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from danswer.configs.app_configs import MASK_CREDENTIAL_PREFIX
from danswer.configs.constants import DocumentSource
from danswer.connectors.models import InputType
from danswer.db.models import Connector
from danswer.db.models import ConnectorCredentialPair
from danswer.db.models import Credential
from danswer.db.models import IndexAttempt
from danswer.db.models import IndexingStatus
from danswer.db.models import TaskStatus
from danswer.server.utils import mask_credential_dict


class DocumentInfo(BaseModel):
    num_chunks: int
    num_tokens: int


class ChunkInfo(BaseModel):
    content: str
    num_tokens: int


class IndexAttemptSnapshot(BaseModel):
    id: int
    status: IndexingStatus | None
    new_docs_indexed: int  # only includes completely new docs
    total_docs_indexed: int  # includes docs that are updated
    docs_removed_from_index: int
    error_msg: str | None
    full_exception_trace: str | None
    time_started: str | None
    time_updated: str
    indexing_priority: int = 0

    @classmethod
    def from_index_attempt_db_model(
        cls, index_attempt: IndexAttempt
    ) -> "IndexAttemptSnapshot":
        return IndexAttemptSnapshot(
            id=index_attempt.id,
            status=index_attempt.status,
            new_docs_indexed=index_attempt.new_docs_indexed or 0,
            total_docs_indexed=index_attempt.total_docs_indexed or 0,
            docs_removed_from_index=index_attempt.docs_removed_from_index or 0,
            error_msg=index_attempt.error_msg,
            full_exception_trace=index_attempt.full_exception_trace,
            time_started=index_attempt.time_started.isoformat()
            if index_attempt.time_started
            else None,
            time_updated=index_attempt.time_updated.isoformat(),
            indexing_priority=getattr(index_attempt, "indexing_priority", 0) or 0,
        )


class DeletionAttemptSnapshot(BaseModel):
    connector_id: int
    credential_id: int
    status: TaskStatus


class ConnectorBase(BaseModel):
    name: str
    source: DocumentSource
    input_type: InputType
    connector_specific_config: dict[str, Any]
    refresh_freq: int | None  # In seconds, None for one time index with no refresh
    prune_freq: int | None
    disabled: bool


class ConnectorSnapshot(ConnectorBase):
    id: int
    credential_ids: list[int]
    time_created: datetime
    time_updated: datetime

    @classmethod
    def from_connector_db_model(cls, connector: Connector) -> "ConnectorSnapshot":
        return ConnectorSnapshot(
            id=connector.id,
            name=connector.name,
            source=connector.source,
            input_type=connector.input_type,
            connector_specific_config=connector.connector_specific_config,
            refresh_freq=connector.refresh_freq,
            prune_freq=connector.prune_freq,
            credential_ids=[
                association.credential.id for association in connector.credentials
            ],
            time_created=connector.time_created,
            time_updated=connector.time_updated,
            disabled=connector.disabled,
        )


class CredentialBase(BaseModel):
    credential_json: dict[str, Any]
    # if `true`, then all Admins will have access to the credential
    admin_public: bool


class CredentialSnapshot(CredentialBase):
    id: int
    user_id: UUID | None
    time_created: datetime
    time_updated: datetime

    @classmethod
    def from_credential_db_model(cls, credential: Credential) -> "CredentialSnapshot":
        return CredentialSnapshot(
            id=credential.id,
            credential_json=mask_credential_dict(credential.credential_json)
            if MASK_CREDENTIAL_PREFIX
            else credential.credential_json,
            user_id=credential.user_id,
            admin_public=credential.admin_public,
            time_created=credential.time_created,
            time_updated=credential.time_updated,
        )


class CCPairFullInfo(BaseModel):
    id: int
    name: str
    num_docs_indexed: int
    connector: ConnectorSnapshot
    credential: CredentialSnapshot
    # The full index-attempt history is paginated via a dedicated endpoint
    # (GET /admin/cc-pair/{id}/index-attempts) — embedding it here loaded a
    # busy cc-pair's entire history (thousands of rows w/ full tracebacks) on
    # every page view. The detail page only needs the latest attempt + a count.
    latest_index_attempt: IndexAttemptSnapshot | None
    num_index_attempts: int
    latest_deletion_attempt: DeletionAttemptSnapshot | None

    @classmethod
    def from_models(
        cls,
        cc_pair_model: ConnectorCredentialPair,
        latest_index_attempt: IndexAttempt | None,
        num_index_attempts: int,
        latest_deletion_attempt: DeletionAttemptSnapshot | None,
        num_docs_indexed: int,  # not ideal, but this must be computed separately
    ) -> "CCPairFullInfo":
        return cls(
            id=cc_pair_model.id,
            name=cc_pair_model.name,
            num_docs_indexed=num_docs_indexed,
            connector=ConnectorSnapshot.from_connector_db_model(
                cc_pair_model.connector
            ),
            credential=CredentialSnapshot.from_credential_db_model(
                cc_pair_model.credential
            ),
            latest_index_attempt=(
                IndexAttemptSnapshot.from_index_attempt_db_model(latest_index_attempt)
                if latest_index_attempt is not None
                else None
            ),
            num_index_attempts=num_index_attempts,
            latest_deletion_attempt=latest_deletion_attempt,
        )


class PaginatedIndexAttempts(BaseModel):
    index_attempts: list[IndexAttemptSnapshot]
    page: int
    total_pages: int
    total_count: int

    @classmethod
    def from_models(
        cls,
        index_attempt_models: list[IndexAttempt],
        page: int,
        total_pages: int,
        total_count: int,
    ) -> "PaginatedIndexAttempts":
        return cls(
            index_attempts=[
                IndexAttemptSnapshot.from_index_attempt_db_model(m)
                for m in index_attempt_models
            ],
            page=page,
            total_pages=total_pages,
            total_count=total_count,
        )


class ConnectorIndexingStatus(BaseModel):
    """Represents the latest indexing status of a connector"""

    cc_pair_id: int
    name: str | None
    connector: ConnectorSnapshot
    credential: CredentialSnapshot
    owner: str
    public_doc: bool
    last_status: IndexingStatus | None
    last_success: datetime | None
    docs_indexed: int
    error_msg: str | None
    latest_index_attempt: IndexAttemptSnapshot | None
    deletion_attempt: DeletionAttemptSnapshot | None
    is_deletable: bool


class ConnectorCredentialPairIdentifier(BaseModel):
    connector_id: int
    credential_id: int


class ConnectorCredentialPairMetadata(BaseModel):
    name: str | None
    is_public: bool


class ConnectorCredentialPairDescriptor(BaseModel):
    id: int
    name: str | None
    connector: ConnectorSnapshot
    credential: CredentialSnapshot


class RunConnectorRequest(BaseModel):
    connector_id: int
    credential_ids: list[int] | None
    from_beginning: bool = False
    # Optional priority for the resulting index attempt(s). Higher values
    # are dispatched first by the indexing scheduler. 0 = normal (default
    # for auto-scheduled runs), conventional ceiling = 10.
    indexing_priority: int = 0


class UpdateIndexAttemptPriorityRequest(BaseModel):
    indexing_priority: int


"""Connectors Models"""


class GoogleAppWebCredentials(BaseModel):
    client_id: str
    project_id: str
    auth_uri: str
    token_uri: str
    auth_provider_x509_cert_url: str
    client_secret: str
    redirect_uris: list[str]
    javascript_origins: list[str]


class GoogleAppCredentials(BaseModel):
    web: GoogleAppWebCredentials


class GoogleServiceAccountKey(BaseModel):
    type: str
    project_id: str
    private_key_id: str
    private_key: str
    client_email: str
    client_id: str
    auth_uri: str
    token_uri: str
    auth_provider_x509_cert_url: str
    client_x509_cert_url: str
    universe_domain: str


class GoogleServiceAccountCredentialRequest(BaseModel):
    google_drive_delegated_user: str | None  # email of user to impersonate
    gmail_delegated_user: str | None  # email of user to impersonate


class FileUploadResponse(BaseModel):
    file_paths: list[str]


class ObjectCreationIdResponse(BaseModel):
    id: int | str


class AuthStatus(BaseModel):
    authenticated: bool


class AuthUrl(BaseModel):
    auth_url: str


class GmailCallback(BaseModel):
    state: str
    code: str


class GDriveCallback(BaseModel):
    state: str
    code: str
