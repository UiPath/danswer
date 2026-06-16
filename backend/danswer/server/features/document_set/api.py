from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from danswer.auth.api_key import validate_api_key
from danswer.auth.users import current_admin_user
from danswer.auth.users import current_user
from danswer.db.document_set import check_document_sets_are_public
from danswer.db.document_set import fetch_all_document_sets
from danswer.db.document_set import insert_document_set
from danswer.db.document_set import mark_document_set_as_to_be_deleted
from danswer.db.document_set import update_document_set
from danswer.db.document_set_cache import get_document_sets_for_user_cached
from danswer.db.engine import get_session
from danswer.db.models import User
from danswer.server.features.document_set.models import CheckDocSetPublicRequest
from danswer.server.features.document_set.models import CheckDocSetPublicResponse
from danswer.server.features.document_set.models import DocumentSet
from danswer.server.features.document_set.models import DocumentSetCreationRequest
from danswer.server.features.document_set.models import DocumentSetUpdateRequest
from danswer.server.utils import user_facing_http_exception

router = APIRouter(prefix="/manage", dependencies=[Depends(validate_api_key)])

# Surfaced when a document-set write hits an IntegrityError. The common cause
# is a document set with both an is_current=True and is_current=False row for
# the same cc-pair (a known data inconsistency); flipping current→outdated then
# collides on the (document_set_id, cc_pair_id, is_current) primary key.
_DOC_SET_INTEGRITY_DETAIL = (
    "This document set has duplicate or conflicting connector entries, so it "
    "couldn't be saved. This is a known data inconsistency — please contact an "
    "administrator to clean it up."
)


@router.post("/admin/document-set")
def create_document_set(
    document_set_creation_request: DocumentSetCreationRequest,
    user: User = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> int:
    try:
        document_set_db_model, _ = insert_document_set(
            document_set_creation_request=document_set_creation_request,
            user_id=user.id if user else None,
            db_session=db_session,
        )
    except Exception as e:
        raise user_facing_http_exception(
            e, "create the document set", integrity_detail=_DOC_SET_INTEGRITY_DETAIL
        )
    return document_set_db_model.id


@router.patch("/admin/document-set")
def patch_document_set(
    document_set_update_request: DocumentSetUpdateRequest,
    _: User = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> None:
    try:
        update_document_set(
            document_set_update_request=document_set_update_request,
            db_session=db_session,
        )
    except Exception as e:
        raise user_facing_http_exception(
            e, "update the document set", integrity_detail=_DOC_SET_INTEGRITY_DETAIL
        )


@router.delete("/admin/document-set/{document_set_id}")
def delete_document_set(
    document_set_id: int,
    _: User = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> None:
    try:
        mark_document_set_as_to_be_deleted(
            document_set_id=document_set_id, db_session=db_session
        )
    except Exception as e:
        raise user_facing_http_exception(e, "delete the document set")


@router.get("/admin/document-set")
def list_document_sets_admin(
    _: User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> list[DocumentSet]:
    return [
        DocumentSet.from_model(ds)
        for ds in fetch_all_document_sets(db_session=db_session)
    ]


"""Endpoints for non-admins"""


@router.get("/document-set")
def list_document_sets(
    user: User | None = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> list[DocumentSet]:
    # Read-through Redis cache (per user, fail-open, default OFF). On the
    # chat-page bundle this fires on every load; the cache collapses a
    # user's repeat loads to one DB build per TTL. The build logic lives in
    # the cache module so cached/uncached paths stay identical.
    return get_document_sets_for_user_cached(
        user_id=user.id if user else None, db_session=db_session
    )


@router.get("/document-set-public")
def document_set_public(
    check_public_request: CheckDocSetPublicRequest,
    _: User = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> CheckDocSetPublicResponse:
    is_public = check_document_sets_are_public(
        document_set_ids=check_public_request.document_set_ids, db_session=db_session
    )
    return CheckDocSetPublicResponse(is_public=is_public)
