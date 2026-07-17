"""Provisioning orchestrator for approved onboarding requests.

On admin approval this turns a validated onboarding payload into real Darwin
resources, in order:
  per source -> Connector (+ credential) -> connector_credential_pair
  -> DocumentSet (over all cc_pairs)
  -> Prompt (Orchestrator's as the default template, overridden by the requester)
  -> Persona (the team's assistant, scoped to the document set)
  -> slack_bot_config (channel -> persona, with SME / oncall / Jira options and
     the requested source order as prioritized_sources)
  -> one high-priority IndexAttempt per cc_pair (so the new sources scrape first)
and records the created ids on the OnboardingRequest for status monitoring.

Payload contract (assembled + validated by the form):
  {
    "team_name": str,
    "channel": {"channel_id": str, "channel_name": str},
    "response_type": "citations" | "quotes",
    "respond_tag_only": bool,
    "system_prompt": str, "task_prompt": str,           # requester-edited
    "sme": {"enabled": bool, "group_name": str},
    "oncall": {"enabled": bool, "schedule": str},       # opsgenie_schedule
    "jira": {"enabled": bool, "project_key": str, "issue_type": str, "component": str},
    "sources": [ {"type": "web"|"confluence"|"github"|"slack",
                  "value": <url|repo-url|channel-name>, "label": str}, ... ]  # priority order
  }
"""
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from danswer.configs.constants import DocumentSource
from danswer.connectors.models import InputType
from danswer.db.connector import create_connector
from danswer.db.connector_credential_pair import add_credential_to_connector
from danswer.db.document_set import insert_document_set
from danswer.db.embedding_model import get_current_db_embedding_model
from danswer.db.index_attempt import create_index_attempt
from danswer.db.models import ChannelConfig
from danswer.db.models import Connector
from danswer.db.models import Credential
from danswer.db.models import OnboardingRequest
from danswer.db.models import OnboardingStatus
from danswer.db.models import Prompt
from danswer.db.models import RecencyBiasSetting
from danswer.db.models import SlackBotResponseType
from danswer.db.models import User
from danswer.db.onboarding import set_provisioned_ids
from danswer.db.onboarding import update_onboarding_status
from danswer.db.persona import get_persona_by_name
from danswer.db.persona import upsert_persona
from danswer.db.persona import upsert_prompt
from danswer.db.slack_bot_config import insert_slack_bot_config
from danswer.server.documents.models import ConnectorBase
from danswer.server.features.document_set.models import DocumentSetCreationRequest
from danswer.utils.logger import setup_logger

logger = setup_logger()

# Onboarded sources scrape ahead of routine re-indexing (IndexAttempt priority 0-100).
ONBOARDING_INDEXING_PRIORITY = 80
DEFAULT_REFRESH_FREQ = 86400  # daily; picks up new docs versions / channel messages
PUBLIC_CREDENTIAL_ID = (
    0  # the empty public credential (create_initial_public_credential)
)
_DEFAULT_TEMPLATE_PERSONA = "Orchestrator"


def _find_credential_id(db_session: Session, required_key: str) -> int | None:
    """Reuse an existing connector's shared credential for auth'd sources
    (Confluence/GitHub/Slack) — the requester never supplies tokens."""
    for cred in db_session.execute(select(Credential)).scalars():
        if (cred.credential_json or {}).get(required_key):
            return cred.id
    return None


def _slack_workspace(db_session: Session) -> str | None:
    for connector in (
        db_session.execute(
            select(Connector).where(Connector.source == DocumentSource.SLACK)
        )
        .scalars()
        .all()
    ):
        ws = (connector.connector_specific_config or {}).get("workspace")
        if ws:
            return ws
    return None


def _parse_github_repo(url: str) -> tuple[str, str]:
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"Not a github repo URL: {url}")
    return parts[0], parts[1]


def _build_connector_base(source: dict, db_session: Session) -> ConnectorBase:
    """One onboarding source -> a ConnectorBase (source-specific config)."""
    stype = source["type"]
    value = source["value"]
    label = source.get("label") or value

    if stype == "web":
        is_uipath_docs = urlparse(value).netloc == "docs.uipath.com"
        config: dict = {"base_url": value, "web_connector_type": "recursive"}
        if is_uipath_docs:
            # Take the root URL and let the connector crawl all product versions.
            config["uipath_latest_versions"] = True
            config["max_versions"] = 2
        return ConnectorBase(
            name=f"[onboarding] {label}",
            source=DocumentSource.WEB,
            input_type=InputType.POLL,
            connector_specific_config=config,
            refresh_freq=DEFAULT_REFRESH_FREQ,
            prune_freq=None,
            disabled=False,
        )
    if stype == "confluence":
        return ConnectorBase(
            name=f"[onboarding] {label}",
            source=DocumentSource.CONFLUENCE,
            input_type=InputType.POLL,
            connector_specific_config={"wiki_page_url": value},
            refresh_freq=DEFAULT_REFRESH_FREQ,
            prune_freq=None,
            disabled=False,
        )
    if stype == "github":
        owner, repo = _parse_github_repo(value)
        return ConnectorBase(
            name=f"[onboarding] {label}",
            source=DocumentSource.GITHUB,
            input_type=InputType.POLL,
            connector_specific_config={
                "repo_owner": owner,
                "repo_name": repo,
                "include_prs": True,
                "include_issues": True,
            },
            refresh_freq=DEFAULT_REFRESH_FREQ,
            prune_freq=None,
            disabled=False,
        )
    if stype == "slack":
        workspace = _slack_workspace(db_session)
        if not workspace:
            raise ValueError("No existing Slack connector to copy the workspace from")
        return ConnectorBase(
            name=f"[onboarding] {label}",
            source=DocumentSource.SLACK,
            input_type=InputType.POLL,
            connector_specific_config={
                "workspace": workspace,
                "channels": [value.lstrip("#")],
                "channel_regex_enabled": False,
            },
            refresh_freq=DEFAULT_REFRESH_FREQ,
            prune_freq=None,
            disabled=False,
        )
    raise ValueError(f"Unsupported source type: {stype}")


# Which decrypted-credential key identifies a reusable credential per source type.
_CREDENTIAL_KEY_BY_SOURCE = {
    "confluence": "confluence_access_token",
    "github": "github_access_token",
    "slack": "slack_bot_token",
}


def _credential_id_for_source(source_type: str, db_session: Session) -> int:
    if source_type == "web":
        return PUBLIC_CREDENTIAL_ID
    key = _CREDENTIAL_KEY_BY_SOURCE.get(source_type)
    if key is None:
        return PUBLIC_CREDENTIAL_ID
    cred_id = _find_credential_id(db_session, key)
    if cred_id is None:
        raise ValueError(
            f"No existing {source_type} credential to reuse (missing '{key}')"
        )
    return cred_id


def _source_type_value(source_type: str) -> str:
    """Onboarding source type -> DocumentSource value (for prioritized_sources)."""
    return {
        "web": DocumentSource.WEB.value,
        "confluence": DocumentSource.CONFLUENCE.value,
        "github": DocumentSource.GITHUB.value,
        "slack": DocumentSource.SLACK.value,
    }.get(source_type, source_type)


def _build_prompt(payload: dict, admin_user: User, db_session: Session) -> Prompt:
    """Create the team's prompt, defaulting to the Orchestrator persona's prompt
    and overriding with whatever the requester edited in the form."""
    template = get_persona_by_name(_DEFAULT_TEMPLATE_PERSONA, admin_user, db_session)
    default_system = ""
    default_task = ""
    if template and template.prompts:
        default_system = template.prompts[0].system_prompt
        default_task = template.prompts[0].task_prompt
    team = payload["team_name"]
    return upsert_prompt(
        user=admin_user,
        name=f"[onboarding] {team} prompt",
        description=f"Prompt for the {team} assistant (from onboarding)",
        system_prompt=payload.get("system_prompt") or default_system,
        task_prompt=payload.get("task_prompt") or default_task,
        include_citations=True,
        datetime_aware=True,
        personas=None,
        db_session=db_session,
        default_prompt=False,
    )


def _build_channel_config(
    payload: dict, prioritized_sources: list[str]
) -> ChannelConfig:
    channel_name = payload["channel"]["channel_name"]
    config: ChannelConfig = {
        "channel_names": [channel_name],
        "respond_tag_only": bool(payload.get("respond_tag_only", False)),
        "prioritized_sources": prioritized_sources,
    }
    sme = payload.get("sme") or {}
    if sme.get("enabled"):
        config["enable_sme_validation"] = True
        config["sme_group_name"] = sme.get("group_name", "")
    oncall = payload.get("oncall") or {}
    if oncall.get("enabled") and oncall.get("schedule"):
        config["opsgenie_schedule"] = oncall["schedule"]
    jira = payload.get("jira") or {}
    if jira.get("enabled"):
        config["jira_config"] = {
            "enable_jira_integration": True,
            "project_key": jira.get("project_key", ""),
            "issue_type": jira.get("issue_type", ""),
            "component": jira.get("component", ""),
        }
    return config


def provision_onboarding(
    request: OnboardingRequest,
    admin_user: User,
    db_session: Session,
) -> OnboardingRequest:
    """Create all resources for an approved request. On any failure the request
    is marked FAILED with the error and the exception is re-raised."""
    payload = request.payload
    update_onboarding_status(
        db_session, request, OnboardingStatus.PROVISIONING, approver_id=admin_user.id
    )
    try:
        embedding_model = get_current_db_embedding_model(db_session)
        team = payload["team_name"]

        # 1) connectors + cc_pairs (track connector/credential ids for indexing).
        cc_pair_ids: list[int] = []
        index_targets: list[tuple[int, int]] = []  # (connector_id, credential_id)
        prioritized_sources: list[str] = []
        for source in payload["sources"]:
            connector = create_connector(
                _build_connector_base(source, db_session), db_session
            )
            connector_id = int(connector.id)
            credential_id = _credential_id_for_source(source["type"], db_session)
            ccp = add_credential_to_connector(
                connector_id=connector_id,
                credential_id=credential_id,
                cc_pair_name=f"[onboarding] {team}: {source.get('label') or source['value']}",
                is_public=True,
                user=admin_user,
                db_session=db_session,
            )
            if ccp.data is None:
                raise ValueError(f"Failed to create cc_pair for {source['value']}")
            cc_pair_ids.append(ccp.data)
            index_targets.append((connector_id, credential_id))
            st = _source_type_value(source["type"])
            if st not in prioritized_sources:
                prioritized_sources.append(st)

        # 2) document set over all cc_pairs.
        doc_set, _ = insert_document_set(
            DocumentSetCreationRequest(
                name=f"[onboarding] {team}",
                description=f"Sources onboarded for {team}",
                cc_pair_ids=cc_pair_ids,
                is_public=True,
            ),
            admin_user.id,
            db_session,
        )

        # 3) prompt (Orchestrator default + requester edits) + persona.
        prompt = _build_prompt(payload, admin_user, db_session)
        persona = upsert_persona(
            user=admin_user,
            name=team,
            description=f"Assistant for {team} (self-serve onboarding)",
            num_chunks=10,
            llm_relevance_filter=False,
            llm_filter_extraction=False,
            recency_bias=RecencyBiasSetting.BASE_DECAY,
            llm_model_provider_override=None,
            llm_model_version_override=None,
            starter_messages=None,
            is_public=True,
            db_session=db_session,
            prompt_ids=[prompt.id],
            document_set_ids=[doc_set.id],
        )

        # 4) slack bot config (channel -> persona) with the options + priority order.
        channel_config = _build_channel_config(payload, prioritized_sources)
        response_type = (
            SlackBotResponseType.QUOTES
            if payload.get("response_type") == "quotes"
            else SlackBotResponseType.CITATIONS
        )
        slack_config = insert_slack_bot_config(
            persona_id=persona.id,
            channel_config=channel_config,
            response_type=response_type,
            db_session=db_session,
        )

        set_provisioned_ids(
            db_session,
            request,
            persona_id=persona.id,
            document_set_id=doc_set.id,
            slack_bot_config_id=slack_config.id,
            cc_pair_ids=cc_pair_ids,
        )

        # 5) kick off high-priority indexing for each new source.
        for connector_id, credential_id in index_targets:
            create_index_attempt(
                connector_id=connector_id,
                credential_id=credential_id,
                embedding_model_id=embedding_model.id,
                db_session=db_session,
                from_beginning=True,
                indexing_priority=ONBOARDING_INDEXING_PRIORITY,
            )

        update_onboarding_status(db_session, request, OnboardingStatus.INDEXING)
        logger.info(
            "onboarding %s provisioned: persona=%s doc_set=%s config=%s cc_pairs=%s",
            request.id,
            persona.id,
            doc_set.id,
            slack_config.id,
            cc_pair_ids,
        )
        return request
    except Exception as e:
        logger.exception("onboarding %s provisioning failed", request.id)
        update_onboarding_status(
            db_session, request, OnboardingStatus.FAILED, error_msg=str(e)
        )
        raise
