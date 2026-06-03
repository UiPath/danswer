from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from danswer.auth.api_key import validate_api_key
from danswer.auth.users import current_admin_user
from danswer.auth.users import current_user
from danswer.background.celery.celery_utils import get_deletion_status
from danswer.db.connector_credential_pair import add_credential_to_connector
from danswer.db.connector_credential_pair import get_connector_credential_pair_from_id
from danswer.db.connector_credential_pair import remove_credential_from_connector
from danswer.db.document import get_document_cnts_for_cc_pairs
from danswer.db.engine import get_session
from danswer.db.index_attempt import count_index_attempts_for_cc_pair
from danswer.db.index_attempt import get_index_attempts_for_cc_pair
from danswer.db.index_attempt import get_paginated_index_attempts_for_cc_pair
from danswer.db.models import User
from danswer.server.documents.models import CCPairFullInfo
from danswer.server.documents.models import ConnectorCredentialPairIdentifier
from danswer.server.documents.models import ConnectorCredentialPairMetadata
from danswer.server.documents.models import PaginatedIndexAttempts
from danswer.server.models import StatusResponse

router = APIRouter(prefix="/manage", dependencies=[Depends(validate_api_key)])


@router.get("/admin/cc-pair/{cc_pair_id}")
def get_cc_pair_full_info(
    cc_pair_id: int,
    _: User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> CCPairFullInfo:
    cc_pair = get_connector_credential_pair_from_id(
        cc_pair_id=cc_pair_id,
        db_session=db_session,
    )
    if cc_pair is None:
        raise HTTPException(
            status_code=400,
            detail=f"Connector with ID {cc_pair_id} not found. Has it been deleted?",
        )

    cc_pair_identifier = ConnectorCredentialPairIdentifier(
        connector_id=cc_pair.connector_id,
        credential_id=cc_pair.credential_id,
    )

    # Only the latest attempt + a count are needed for the detail page; the
    # full history is served (paginated) by the endpoint below.
    latest_index_attempts = get_index_attempts_for_cc_pair(
        db_session=db_session,
        cc_pair_identifier=cc_pair_identifier,
        limit=1,
    )
    latest_index_attempt = latest_index_attempts[0] if latest_index_attempts else None
    num_index_attempts = count_index_attempts_for_cc_pair(
        db_session=db_session,
        cc_pair_identifier=cc_pair_identifier,
    )

    document_count_info_list = list(
        get_document_cnts_for_cc_pairs(
            db_session=db_session,
            cc_pair_identifiers=[cc_pair_identifier],
        )
    )
    documents_indexed = (
        document_count_info_list[0][-1] if document_count_info_list else 0
    )

    latest_deletion_attempt = get_deletion_status(
        connector_id=cc_pair.connector.id,
        credential_id=cc_pair.credential.id,
        db_session=db_session,
    )

    return CCPairFullInfo.from_models(
        cc_pair_model=cc_pair,
        latest_index_attempt=latest_index_attempt,
        num_index_attempts=num_index_attempts,
        latest_deletion_attempt=latest_deletion_attempt,
        num_docs_indexed=documents_indexed,
    )


@router.get("/admin/cc-pair/{cc_pair_id}/index-attempts")
def get_cc_pair_index_attempts(
    cc_pair_id: int,
    page: int = 0,
    page_size: int = 10,
    _: User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> PaginatedIndexAttempts:
    cc_pair = get_connector_credential_pair_from_id(
        cc_pair_id=cc_pair_id,
        db_session=db_session,
    )
    if cc_pair is None:
        raise HTTPException(
            status_code=400,
            detail=f"Connector with ID {cc_pair_id} not found. Has it been deleted?",
        )

    page = max(page, 0)
    page_size = min(max(page_size, 1), 100)  # clamp to a sane range

    cc_pair_identifier = ConnectorCredentialPairIdentifier(
        connector_id=cc_pair.connector_id,
        credential_id=cc_pair.credential_id,
    )

    total_count = count_index_attempts_for_cc_pair(
        db_session=db_session,
        cc_pair_identifier=cc_pair_identifier,
    )
    index_attempts = get_paginated_index_attempts_for_cc_pair(
        db_session=db_session,
        cc_pair_identifier=cc_pair_identifier,
        page=page,
        page_size=page_size,
    )
    total_pages = max((total_count + page_size - 1) // page_size, 1)

    return PaginatedIndexAttempts.from_models(
        index_attempt_models=list(index_attempts),
        page=page,
        total_pages=total_pages,
        total_count=total_count,
    )


class CCPairRenameRequest(BaseModel):
    name: str


@router.put("/admin/cc-pair/{cc_pair_id}/name")
def rename_cc_pair(
    cc_pair_id: int,
    request: CCPairRenameRequest,
    _: User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> StatusResponse[int]:
    new_name = request.name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")

    cc_pair = get_connector_credential_pair_from_id(
        cc_pair_id=cc_pair_id,
        db_session=db_session,
    )
    if cc_pair is None:
        raise HTTPException(
            status_code=404,
            detail=f"Connector with ID {cc_pair_id} not found",
        )

    cc_pair.name = new_name
    db_session.commit()

    return StatusResponse(
        success=True,
        message="Connector renamed",
        data=cc_pair_id,
    )


@router.put("/connector/{connector_id}/credential/{credential_id}")
def associate_credential_to_connector(
    connector_id: int,
    credential_id: int,
    metadata: ConnectorCredentialPairMetadata,
    user: User = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> StatusResponse[int]:
    try:
        return add_credential_to_connector(
            connector_id=connector_id,
            credential_id=credential_id,
            cc_pair_name=metadata.name,
            is_public=metadata.is_public,
            user=user,
            db_session=db_session,
        )
    except IntegrityError:
        raise HTTPException(status_code=400, detail="Name must be unique")


@router.delete("/connector/{connector_id}/credential/{credential_id}")
def dissociate_credential_from_connector(
    connector_id: int,
    credential_id: int,
    user: User = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> StatusResponse[int]:
    return remove_credential_from_connector(
        connector_id, credential_id, user, db_session
    )
