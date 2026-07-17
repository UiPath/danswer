"""Self-serve onboarding API.

- basic_router (/onboarding): any authenticated user may submit a request, watch
  their own requests, validate form fields inline, and monitor scrape status.
- admin_router (/admin/onboarding): admins list/approve/reject; approval triggers
  provisioning (connectors + assistant + Slack config + high-priority indexing).
"""
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy import desc
from sqlalchemy import select
from sqlalchemy.orm import Session

from danswer.auth.users import current_admin_user
from danswer.auth.users import current_user
from danswer.configs.app_configs import DISABLE_AUTH
from danswer.db.connector_credential_pair import get_connector_credential_pair_from_id
from danswer.db.engine import get_session
from danswer.db.models import IndexAttempt
from danswer.db.models import IndexingStatus
from danswer.db.models import OnboardingRequest
from danswer.db.models import OnboardingStatus
from danswer.db.models import User
from danswer.db.models import UserRole
from danswer.db.onboarding import create_onboarding_request
from danswer.db.onboarding import get_onboarding_request
from danswer.db.onboarding import list_onboarding_requests
from danswer.db.onboarding import list_onboarding_requests_for_user
from danswer.db.onboarding import update_onboarding_status
from danswer.onboarding.provision import provision_onboarding
from danswer.onboarding.validation import validate_confluence_url
from danswer.onboarding.validation import validate_docs_url
from danswer.onboarding.validation import validate_github_repo
from danswer.onboarding.validation import validate_jira_filter
from danswer.onboarding.validation import validate_slack_channel
from danswer.onboarding.validation import validate_slack_group
from danswer.onboarding.validation import ValidationResult
from danswer.server.features.onboarding.models import DecisionRequest
from danswer.server.features.onboarding.models import OnboardingRequestSnapshot
from danswer.server.features.onboarding.models import OnboardingStatusResponse
from danswer.server.features.onboarding.models import OnboardingSubmitRequest
from danswer.server.features.onboarding.models import SourceStatus
from danswer.server.features.onboarding.models import ValidateRequest

basic_router = APIRouter(prefix="/onboarding")
admin_router = APIRouter(prefix="/admin/onboarding")


def _require_user(user: User | None) -> None:
    """Onboarding is a human workflow — reject anonymous / API-key (service)
    callers. `current_user` returns None for API-key callers and when auth is
    globally disabled; allow None only in the latter (dev) case."""
    if user is None and not DISABLE_AUTH:
        raise HTTPException(status_code=401, detail="Authentication required.")


# --- submit + validate + monitor (any authed user) --------------------------


@basic_router.post("")
def submit_onboarding(
    request: OnboardingSubmitRequest,
    user: User | None = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> OnboardingRequestSnapshot:
    _require_user(user)
    if not request.sources:
        raise HTTPException(status_code=400, detail="At least one source is required.")
    created = create_onboarding_request(
        db_session=db_session,
        requester_id=user.id if user else None,
        requester_email=user.email if user else "system@darwin",
        payload=request.to_payload(),
    )
    return OnboardingRequestSnapshot.from_model(created)


@basic_router.post("/validate")
def validate_field(
    request: ValidateRequest,
    user: User | None = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> ValidationResult:
    """Inline validation for a single form field."""
    _require_user(user)
    if request.kind == "slack_channel":
        return validate_slack_channel(request.value)
    if request.kind == "slack_group":
        return validate_slack_group(request.value)
    if request.kind == "confluence":
        return validate_confluence_url(request.value, db_session)
    if request.kind == "github":
        return validate_github_repo(request.value, db_session)
    if request.kind == "jira":
        return validate_jira_filter(request.value, db_session)
    return validate_docs_url(request.value)


@basic_router.get("/default-prompt")
def default_prompt(
    user: User | None = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> dict:
    """The template prompt the form prefills (the Orchestrator assistant's prompt);
    the requester can edit it before submitting."""
    from danswer.db.persona import get_persona_by_name

    template = get_persona_by_name("Orchestrator", user, db_session)
    if template and template.prompts:
        p = template.prompts[0]
        return {"system_prompt": p.system_prompt, "task_prompt": p.task_prompt}
    return {"system_prompt": "", "task_prompt": ""}


@basic_router.get("/mine")
def my_onboarding_requests(
    user: User | None = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> list[OnboardingRequestSnapshot]:
    if user is None:
        return []
    return [
        OnboardingRequestSnapshot.from_model(r)
        for r in list_onboarding_requests_for_user(db_session, user.id)
    ]


def _source_statuses(
    request: OnboardingRequest, db_session: Session
) -> list[SourceStatus]:
    """Per-cc_pair scrape status for the request (from the latest IndexAttempt)."""
    statuses: list[SourceStatus] = []
    for cc_pair_id in request.cc_pair_ids or []:
        cc_pair = get_connector_credential_pair_from_id(cc_pair_id, db_session)
        if cc_pair is None:
            continue
        latest = db_session.execute(
            select(IndexAttempt)
            .where(IndexAttempt.connector_id == cc_pair.connector_id)
            .where(IndexAttempt.credential_id == cc_pair.credential_id)
            .order_by(desc(IndexAttempt.time_created))
            .limit(1)
        ).scalar_one_or_none()
        statuses.append(
            SourceStatus(
                cc_pair_id=cc_pair_id,
                name=cc_pair.name,
                status=latest.status.value if latest else "not_started",
                docs_indexed=(latest.total_docs_indexed or 0) if latest else 0,
                error_msg=latest.error_msg if latest else None,
            )
        )
    return statuses


@basic_router.get("/{request_id}/status")
def onboarding_status(
    request_id: int,
    user: User | None = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> OnboardingStatusResponse:
    _require_user(user)
    request = get_onboarding_request(db_session, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Onboarding request not found.")
    # Requester or an admin may view.
    if (
        user is not None
        and user.role != UserRole.ADMIN
        and request.requester_id != user.id
    ):
        raise HTTPException(status_code=403, detail="Not your onboarding request.")

    sources = _source_statuses(request, db_session)
    # Auto-advance INDEXING -> COMPLETE once every source has indexed successfully.
    if (
        request.status == OnboardingStatus.INDEXING.value
        and sources
        and all(s.status == IndexingStatus.SUCCESS.value for s in sources)
    ):
        update_onboarding_status(db_session, request, OnboardingStatus.COMPLETE)
    return OnboardingStatusResponse(
        request_id=request.id, status=request.status, sources=sources
    )


# --- admin approval ---------------------------------------------------------


@admin_router.get("")
def list_requests(
    _: User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> list[OnboardingRequestSnapshot]:
    return [
        OnboardingRequestSnapshot.from_model(r)
        for r in list_onboarding_requests(db_session)
    ]


@admin_router.post("/{request_id}/approve")
def approve_request(
    request_id: int,
    admin: User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> OnboardingRequestSnapshot:
    request = get_onboarding_request(db_session, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Onboarding request not found.")
    if request.status != OnboardingStatus.PENDING.value:
        raise HTTPException(
            status_code=400,
            detail=f"Request is '{request.status}', only pending requests can be approved.",
        )
    if admin is None:
        raise HTTPException(
            status_code=403, detail="Admin identity required to approve."
        )
    try:
        provision_onboarding(request, admin, db_session)
    except Exception as e:
        # provision_onboarding already marked the request FAILED; surface the error.
        raise HTTPException(status_code=500, detail=f"Provisioning failed: {e}")
    return OnboardingRequestSnapshot.from_model(request)


@admin_router.post("/{request_id}/reject")
def reject_request(
    request_id: int,
    decision: DecisionRequest,
    admin: User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> OnboardingRequestSnapshot:
    request = get_onboarding_request(db_session, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Onboarding request not found.")
    if request.status != OnboardingStatus.PENDING.value:
        raise HTTPException(
            status_code=400,
            detail=f"Request is '{request.status}', only pending requests can be rejected.",
        )
    update_onboarding_status(
        db_session,
        request,
        OnboardingStatus.REJECTED,
        approver_id=admin.id if admin else None,
        decision_reason=decision.reason,
    )
    return OnboardingRequestSnapshot.from_model(request)
