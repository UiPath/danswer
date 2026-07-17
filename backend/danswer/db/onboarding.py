"""DB helpers for the self-serve Darwin onboarding flow (see OnboardingRequest).

Status is stored as the enum VALUE string; always write/compare with
OnboardingStatus(...).value to stay consistent with the column + migration."""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from danswer.db.models import OnboardingRequest
from danswer.db.models import OnboardingStatus


def create_onboarding_request(
    db_session: Session,
    requester_id: UUID | None,
    requester_email: str,
    payload: dict,
) -> OnboardingRequest:
    request = OnboardingRequest(
        requester_id=requester_id,
        requester_email=requester_email,
        payload=payload,
        status=OnboardingStatus.PENDING.value,
    )
    db_session.add(request)
    db_session.commit()
    return request


def get_onboarding_request(
    db_session: Session, request_id: int
) -> OnboardingRequest | None:
    return db_session.get(OnboardingRequest, request_id)


def list_onboarding_requests(
    db_session: Session, status: OnboardingStatus | None = None
) -> list[OnboardingRequest]:
    """All requests (admin view), newest first; optionally filtered by status."""
    stmt = select(OnboardingRequest).order_by(OnboardingRequest.created_at.desc())
    if status is not None:
        stmt = stmt.where(OnboardingRequest.status == status.value)
    return list(db_session.execute(stmt).scalars().all())


def list_onboarding_requests_for_user(
    db_session: Session, requester_id: UUID
) -> list[OnboardingRequest]:
    """A requester's own submissions, newest first."""
    stmt = (
        select(OnboardingRequest)
        .where(OnboardingRequest.requester_id == requester_id)
        .order_by(OnboardingRequest.created_at.desc())
    )
    return list(db_session.execute(stmt).scalars().all())


def update_onboarding_status(
    db_session: Session,
    request: OnboardingRequest,
    status: OnboardingStatus,
    *,
    approver_id: UUID | None = None,
    decision_reason: str | None = None,
    error_msg: str | None = None,
    commit: bool = True,
) -> OnboardingRequest:
    request.status = status.value
    if approver_id is not None:
        request.approver_id = approver_id
    if decision_reason is not None:
        request.decision_reason = decision_reason
    # error_msg is cleared on a non-failed transition, set on failure.
    request.error_msg = error_msg if status == OnboardingStatus.FAILED else None
    if commit:
        db_session.commit()
    return request


def update_onboarding_payload(
    db_session: Session, request: OnboardingRequest, payload: dict
) -> OnboardingRequest:
    """Replace a request's payload (admin edits a pending request before approving).
    JSONB is only re-persisted on reassignment, so set a fresh dict."""
    request.payload = dict(payload)
    db_session.commit()
    return request


def set_provisioned_ids(
    db_session: Session,
    request: OnboardingRequest,
    *,
    persona_id: int | None = None,
    document_set_id: int | None = None,
    slack_bot_config_id: int | None = None,
    cc_pair_ids: list[int] | None = None,
    commit: bool = True,
) -> OnboardingRequest:
    """Record the resources provisioning created, so the status monitor can map
    the request to its cc_pairs / assistant."""
    if persona_id is not None:
        request.persona_id = persona_id
    if document_set_id is not None:
        request.document_set_id = document_set_id
    if slack_bot_config_id is not None:
        request.slack_bot_config_id = slack_bot_config_id
    if cc_pair_ids is not None:
        request.cc_pair_ids = cc_pair_ids
    if commit:
        db_session.commit()
    return request
