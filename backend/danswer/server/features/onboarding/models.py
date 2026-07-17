"""Request/response models for the self-serve onboarding API."""
import datetime
from typing import Literal

from pydantic import BaseModel

from danswer.db.models import OnboardingRequest


class ChannelRef(BaseModel):
    channel_id: str
    channel_name: str


class OnboardingSourceModel(BaseModel):
    # Priority is the order in the sources list (index 0 = highest).
    type: Literal["web", "confluence", "github", "slack"]
    value: str  # docs root URL / confluence URL / github repo URL / slack channel name
    label: str | None = None


class SmeOption(BaseModel):
    enabled: bool = False
    group_name: str = ""  # comma-separated Slack user-group names/@handles


class OncallOption(BaseModel):
    enabled: bool = False
    schedule: str = ""  # opsgenie schedule name to tag on "need more help"


class JiraOption(BaseModel):
    enabled: bool = False
    project_key: str = ""
    issue_type: str = ""
    component: str = ""


class OnboardingSubmitRequest(BaseModel):
    team_name: str
    channel: ChannelRef
    response_type: Literal["citations", "quotes"] = "citations"
    respond_tag_only: bool = False
    system_prompt: str = ""
    task_prompt: str = ""
    sme: SmeOption = SmeOption()
    oncall: OncallOption = OncallOption()
    jira: JiraOption = JiraOption()
    sources: list[OnboardingSourceModel]

    def to_payload(self) -> dict:
        return self.dict()


class OnboardingRequestSnapshot(BaseModel):
    id: int
    requester_email: str
    status: str
    payload: dict
    decision_reason: str | None
    error_msg: str | None
    persona_id: int | None
    document_set_id: int | None
    slack_bot_config_id: int | None
    cc_pair_ids: list[int] | None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    @classmethod
    def from_model(cls, r: OnboardingRequest) -> "OnboardingRequestSnapshot":
        return cls(
            id=r.id,
            requester_email=r.requester_email,
            status=r.status,
            payload=r.payload,
            decision_reason=r.decision_reason,
            error_msg=r.error_msg,
            persona_id=r.persona_id,
            document_set_id=r.document_set_id,
            slack_bot_config_id=r.slack_bot_config_id,
            cc_pair_ids=r.cc_pair_ids,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )


class ValidateRequest(BaseModel):
    kind: Literal["slack_channel", "slack_group", "confluence", "docs"]
    value: str


class DecisionRequest(BaseModel):
    reason: str | None = None


class SourceStatus(BaseModel):
    cc_pair_id: int
    name: str
    status: str  # IndexingStatus value, or "not_started"
    docs_indexed: int
    error_msg: str | None


class OnboardingStatusResponse(BaseModel):
    request_id: int
    status: str
    sources: list[SourceStatus]
