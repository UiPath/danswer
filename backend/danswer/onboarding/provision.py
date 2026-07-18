"""Provisioning orchestrator for approved onboarding requests.

Two phases, so Darwin only goes live in a channel once it actually has the
knowledge:

Phase 1 — `provision_onboarding` (on admin approval):
  per source -> Connector (+ credential) -> connector_credential_pair
  -> one high-priority IndexAttempt per cc_pair (so the new sources scrape first)
  -> status INDEXING. Redundant child Confluence pages are dropped (parent only);
  per-source scrape cadence is set (Slack/Confluence/Jira daily, docs monthly).

Phase 2 — `finalize_onboarding` (background sweep, once every source is scraped):
  DocumentSet (over the cc_pairs) -> Prompt (Orchestrator default, requester-
  edited) -> Persona (tied to the document set) -> slack_bot_config (channel ->
  persona, SME / oncall / Jira options, prioritized_sources) -> status COMPLETE,
  and a #darwin-devs notification. The sweep (`finalize_ready_onboarding_requests`)
  also flags source failures; it re-checks FAILED requests too, so fixing +
  re-indexing a source lets it complete with nothing to restart.

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
import ipaddress
import socket
from urllib.parse import urlparse

from sqlalchemy import desc
from sqlalchemy import select
from sqlalchemy.orm import Session

from danswer.configs.constants import DocumentSource
from danswer.connectors.confluence.connector import extract_confluence_keys_from_url
from danswer.connectors.models import InputType
from danswer.db.connector import create_connector
from danswer.db.connector_credential_pair import add_credential_to_connector
from danswer.db.connector_credential_pair import get_connector_credential_pair
from danswer.db.connector_credential_pair import get_connector_credential_pair_from_id
from danswer.db.document_set import insert_document_set
from danswer.db.embedding_model import get_current_db_embedding_model
from danswer.db.index_attempt import create_index_attempt
from danswer.db.models import ChannelConfig
from danswer.db.models import Connector
from danswer.db.models import Credential
from danswer.db.models import IndexAttempt
from danswer.db.models import IndexingStatus
from danswer.db.models import OnboardingRequest
from danswer.db.models import OnboardingStatus
from danswer.db.models import Prompt
from danswer.db.models import RecencyBiasSetting
from danswer.db.models import SlackBotResponseType
from danswer.db.models import Tool
from danswer.db.models import User
from danswer.db.onboarding import list_onboarding_requests
from danswer.db.onboarding import set_provisioned_ids
from danswer.db.onboarding import update_onboarding_status
from danswer.db.persona import get_persona_by_name
from danswer.db.persona import upsert_persona
from danswer.db.persona import upsert_prompt
from danswer.db.slack_bot_config import insert_slack_bot_config
from danswer.onboarding.notify import notify_client_approved
from danswer.onboarding.notify import notify_client_complete
from danswer.onboarding.notify import notify_client_failed
from danswer.onboarding.notify import notify_onboarding_complete
from danswer.onboarding.notify import notify_onboarding_failed
from danswer.onboarding.validation import _allowed_confluence_hosts
from danswer.onboarding.validation import confluence_page_ancestors
from danswer.onboarding.validation import jira_base_url
from danswer.onboarding.validation import validate_confluence_url
from danswer.onboarding.validation import validate_github_repo
from danswer.onboarding.validation import validate_jira_filter
from danswer.onboarding.validation import validate_slack_channel
from danswer.server.documents.models import ConnectorBase
from danswer.server.features.document_set.models import DocumentSetCreationRequest
from danswer.utils.logger import setup_logger

logger = setup_logger()

# Onboarded sources scrape ahead of routine re-indexing (IndexAttempt priority 0-100).
# in_code_tool_id of the built-in document SearchTool (Tool.in_code_tool_id ==
# SearchTool.__name__). Kept as a literal to avoid importing the heavy search
# pipeline into this module.
_SEARCH_TOOL_IN_CODE_ID = "SearchTool"

ONBOARDING_INDEXING_PRIORITY = 80
DEFAULT_REFRESH_FREQ = 86400  # daily
_MONTHLY_REFRESH_FREQ = 2592000  # 30 days

# Per-source scrape cadence: Slack / Confluence / Jira change often (daily); docs
# sites change slowly and are large, so monthly.
REFRESH_FREQ_BY_SOURCE = {
    "slack": DEFAULT_REFRESH_FREQ,
    "confluence": DEFAULT_REFRESH_FREQ,
    "jira": DEFAULT_REFRESH_FREQ,
    "web": _MONTHLY_REFRESH_FREQ,  # docs
}
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


# Hosts we accept for GitHub sources. The connector authenticates against the
# GitHub API (not the URL host), but pinning the host keeps non-GitHub URLs out.
_ALLOWED_GITHUB_HOSTS = {"github.com", "www.github.com"}


def _assert_public_web_host(url: str) -> None:
    """SSRF guard for web sources: require an http(s) URL whose host does not
    resolve to a private / loopback / link-local / reserved address, so an
    onboarding request can't point the crawler at internal infrastructure.

    Fails closed: a URL we can't parse or resolve is rejected rather than
    fetched. This is a provision-time check; it doesn't defend against DNS
    rebinding at fetch time, but it closes the obvious internal-target vector."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Web source URL must be http(s): {url}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"Web source URL has no host: {url}")
    try:
        addr_infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise ValueError(f"Could not resolve web source host '{host}': {e}")
    for info in addr_infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                f"Web source host '{host}' resolves to a non-public address ({ip})"
            )


def _assert_source_accessible(source: dict, db_session: Session) -> None:
    """Fail-fast at provision: confirm the reusable credential can actually reach
    the source, so we never create a connector that would silently index nothing.
    Reuses the same validators as the submit-time /validate endpoint. Web sources
    are public (no credential) and are host-guarded in `_build_connector_base`."""
    stype = source["type"]
    value = source["value"]
    if stype == "confluence":
        result = validate_confluence_url(value, db_session)
    elif stype == "github":
        result = validate_github_repo(value, db_session)
    elif stype == "slack":
        result = validate_slack_channel(value)
    elif stype == "jira":
        result = validate_jira_filter(value, db_session)
    else:
        return
    if not result.valid:
        raise ValueError(f"{stype} source not accessible: {result.message}")


def _build_connector_base(source: dict, db_session: Session) -> ConnectorBase:
    """One onboarding source -> a ConnectorBase (source-specific config)."""
    stype = source["type"]
    value = source["value"]
    label = source.get("label") or value
    refresh_freq = REFRESH_FREQ_BY_SOURCE.get(stype, DEFAULT_REFRESH_FREQ)

    if stype == "web":
        # SSRF guard: never let a submitted URL point the crawler at internal
        # hosts (validation at submit time can be bypassed by a direct API call).
        _assert_public_web_host(value)
        is_uipath_docs = urlparse(value).netloc == "docs.uipath.com"
        config: dict = {"base_url": value, "web_connector_type": "recursive"}
        if is_uipath_docs:
            # Strip the version from the root URL and crawl the latest few
            # concrete versions (no need to list each version URL).
            config["uipath_latest_versions"] = True
            config["max_versions"] = 3
        return ConnectorBase(
            name=f"[onboarding] {label}",
            source=DocumentSource.WEB,
            input_type=InputType.POLL,
            connector_specific_config=config,
            refresh_freq=refresh_freq,
            prune_freq=None,
            disabled=False,
        )
    if stype == "confluence":
        # Credential-leak / SSRF guard: provisioning creates a connector that
        # will authenticate to this host with our stored Confluence token, so
        # only allow a host an existing Confluence connector already uses. Re-run
        # here (not just at submit) because provisioning is the point the
        # credential is actually used, and submit-time validation can be skipped.
        host = urlparse(value).netloc.lower()
        allowed_hosts = _allowed_confluence_hosts(db_session)
        if not allowed_hosts or host not in allowed_hosts:
            raise ValueError(
                f"Confluence host '{host}' is not allowed — it must match an "
                f"existing Confluence connector ({sorted(allowed_hosts)})"
            )
        return ConnectorBase(
            name=f"[onboarding] {label}",
            source=DocumentSource.CONFLUENCE,
            input_type=InputType.POLL,
            connector_specific_config={"wiki_page_url": value},
            refresh_freq=refresh_freq,
            prune_freq=None,
            disabled=False,
        )
    if stype == "github":
        gh_host = (urlparse(value).hostname or "").lower()
        if gh_host not in _ALLOWED_GITHUB_HOSTS:
            raise ValueError(f"GitHub source URL must be on github.com: {value}")
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
            refresh_freq=refresh_freq,
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
            refresh_freq=refresh_freq,
            prune_freq=None,
            disabled=False,
        )
    if stype == "jira":
        # value is a JQL filter; reuse an existing Jira connector's base URL so
        # the requester never supplies the host/credentials.
        base = jira_base_url(db_session)
        if not base:
            raise ValueError("No existing Jira connector to copy the base URL from")
        return ConnectorBase(
            name=f"[onboarding] {label}",
            source=DocumentSource.JIRA,
            input_type=InputType.POLL,
            connector_specific_config={"jira_base_url": base, "jira_filter": value},
            refresh_freq=refresh_freq,
            prune_freq=None,
            disabled=False,
        )
    raise ValueError(f"Unsupported source type: {stype}")


# Which decrypted-credential key identifies a reusable credential per source type.
_CREDENTIAL_KEY_BY_SOURCE = {
    "confluence": "confluence_access_token",
    "github": "github_access_token",
    "slack": "slack_bot_token",
    "jira": "jira_api_token",
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
        "jira": DocumentSource.JIRA.value,
    }.get(source_type, source_type)


def _build_prompt(
    payload: dict, admin_user: User | None, db_session: Session
) -> Prompt:
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
    if oncall.get("enabled"):
        if oncall.get("schedule"):
            config["opsgenie_schedule"] = oncall["schedule"]
        # DRI Slack handles/emails to tag on "need more help" (follow_up_tags is
        # resolved as emails->users then handles/names->user-groups at runtime).
        handles = [
            h.strip().lstrip("@")
            for h in (oncall.get("handles") or "").split(",")
            if h.strip()
        ]
        if handles:
            config["follow_up_tags"] = handles
    jira = payload.get("jira") or {}
    if jira.get("enabled"):
        config["jira_config"] = {
            "enable_jira_integration": True,
            "project_key": jira.get("project_key", ""),
            "issue_type": jira.get("issue_type", ""),
            "component": jira.get("component", ""),
        }
    return config


def _prioritized_sources(sources: list[dict]) -> list[str]:
    """Distinct DocumentSource values in the requester's priority order."""
    ordered: list[str] = []
    for s in sources:
        st = _source_type_value(s["type"])
        if st not in ordered:
            ordered.append(st)
    return ordered


def _dedup_confluence_sources(sources: list[dict], db_session: Session) -> list[dict]:
    """Drop child Confluence pages when a parent is also provided, keeping the
    parent alone (the connector already recurses a page's descendants). A whole-
    space URL supersedes any page in that space; a parent page supersedes its
    descendant pages (best-effort via the Confluence ancestry API)."""
    conf = [(i, s) for i, s in enumerate(sources) if s.get("type") == "confluence"]
    if len(conf) < 2:
        return sources
    keys: dict[int, tuple[str, str]] = {}
    for i, s in conf:
        try:
            _b, space, page_id, _c = extract_confluence_keys_from_url(s["value"])
            keys[i] = (space.lower(), page_id or "")
        except Exception:
            keys[i] = ("", "")
    drop: set[int] = set()

    # A whole-space URL supersedes pages in the same space.
    space_roots = {sp for (sp, pid) in keys.values() if sp and not pid}
    for i, _s in conf:
        sp, pid = keys[i]
        if pid and sp in space_roots:
            drop.add(i)

    # A parent page supersedes its descendant pages (best-effort).
    remaining = [i for i, _s in conf if i not in drop and keys[i][1]]
    if len(remaining) >= 2:
        provided = {keys[i][1] for i in remaining}
        ancestry = confluence_page_ancestors(list(provided), db_session)
        for i in remaining:
            if provided & set(ancestry.get(keys[i][1], [])):
                drop.add(i)  # an ancestor of this page was also provided

    if drop:
        logger.info(
            "onboarding: dropped %d redundant child Confluence source(s)", len(drop)
        )
    return [s for i, s in enumerate(sources) if i not in drop]


def provision_onboarding(
    request: OnboardingRequest,
    admin_user: User,
    db_session: Session,
) -> OnboardingRequest:
    """Phase 1 (on approval): create the connectors + cc_pairs and kick off
    high-priority indexing; status -> INDEXING. The document set, assistant, and
    Slack config are created later by `finalize_onboarding`, once every source
    has finished scraping (so Darwin only goes live once it has real knowledge).
    On any failure here the request is marked FAILED and the exception re-raised."""
    payload = request.payload
    update_onboarding_status(
        db_session, request, OnboardingStatus.PROVISIONING, approver_id=admin_user.id
    )
    try:
        embedding_model = get_current_db_embedding_model(db_session)
        team = payload["team_name"]
        sources = _dedup_confluence_sources(list(payload["sources"]), db_session)

        cc_pair_ids: list[int] = []
        index_targets: list[tuple[int, int]] = []  # (connector_id, credential_id)
        for source in sources:
            # Preflight: the reusable credential must be able to reach the source.
            _assert_source_accessible(source, db_session)
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
            # NB: in this fork add_credential_to_connector returns
            # data=connector_id, NOT the cc_pair id. Look the pair up by
            # (connector, credential) to record its real id — otherwise the
            # request tracks connector ids as if they were cc_pair ids, which
            # (off by one) points the finalizer/status at the wrong cc_pairs.
            cc_pair = get_connector_credential_pair(
                connector_id, credential_id, db_session
            )
            if cc_pair is None:
                raise ValueError(f"Failed to create cc_pair for {source['value']}")
            cc_pair_ids.append(cc_pair.id)
            index_targets.append((connector_id, credential_id))

        set_provisioned_ids(db_session, request, cc_pair_ids=cc_pair_ids)

        # Kick off high-priority indexing for each source.
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
            "onboarding %s provisioned %d source(s); indexing. cc_pairs=%s",
            request.id,
            len(cc_pair_ids),
            cc_pair_ids,
        )
        # Customer thread: reviewed + approved, indexing started.
        notify_client_approved(request)
        return request
    except Exception as e:
        logger.exception("onboarding %s provisioning failed", request.id)
        update_onboarding_status(
            db_session, request, OnboardingStatus.FAILED, error_msg=str(e)
        )
        raise


def _finalize_owner(request: OnboardingRequest, db_session: Session) -> User | None:
    """Owner of the created assistant/doc-set — the approver, else the requester.
    finalize runs in the background, so we resolve it from the row."""
    for uid in (request.approver_id, request.requester_id):
        if uid is not None:
            user = db_session.get(User, uid)
            if user is not None:
                return user
    return None


def finalize_onboarding(
    request: OnboardingRequest, db_session: Session
) -> OnboardingRequest:
    """Phase 2 (after all sources are scraped): create the document set over the
    request's cc_pairs, the assistant (prompt + persona) tied to it, and the Slack
    bot config — making Darwin live in the channel — then mark COMPLETE + notify."""
    payload = request.payload
    owner = _finalize_owner(request, db_session)
    owner_id = owner.id if owner else None
    team = payload["team_name"]
    cc_pair_ids = list(request.cc_pair_ids or [])
    # Capture already-provisioned artifact ids into locals BEFORE dropping the
    # transaction: the idempotency guards below must not re-open one (a read
    # would auto-begin) right before insert_document_set's begin().
    document_set_id = request.document_set_id
    persona_id = request.persona_id
    slack_bot_config_id = request.slack_bot_config_id

    # The finalizer already opened a transaction on this session doing its reads
    # (list / _source_index_state / _finalize_owner above). insert_document_set
    # calls db_session.begin(), which raises "A transaction is already begun"
    # when one is active — so drop the read-only transaction here.
    db_session.rollback()

    # Idempotent + incremental so this survives a pod crash mid-finalize: create
    # each artifact only if the request doesn't already reference one, and
    # persist its id immediately (a checkpoint). A crash between steps resumes on
    # the next finalizer tick instead of duplicating doc sets / personas / Slack
    # configs. Each helper commits, so every persisted id is durable.

    # 1. Document set over the request's cc_pairs.
    if document_set_id is None:
        doc_set, _ = insert_document_set(
            DocumentSetCreationRequest(
                name=f"[onboarding] {team}",
                description=f"Sources onboarded for {team}",
                cc_pair_ids=cc_pair_ids,
                is_public=True,
            ),
            owner_id,
            db_session,
        )
        document_set_id = doc_set.id
        set_provisioned_ids(db_session, request, document_set_id=document_set_id)

    # 2. Assistant (prompt + persona) tied to the document set. The SearchTool
    # MUST be attached or the assistant has the document set but can't actually
    # search it (upsert_persona only enables search when tool_ids includes it;
    # the startup auto-add migration doesn't cover personas created later here).
    if persona_id is None:
        prompt = _build_prompt(payload, owner, db_session)
        search_tool = (
            db_session.query(Tool)
            .filter(Tool.in_code_tool_id == _SEARCH_TOOL_IN_CODE_ID)
            .first()
        )
        if search_tool is None:
            logger.warning(
                "onboarding %s: SearchTool not found — assistant will have the "
                "document set but no search tool",
                request.id,
            )
        tool_ids = [search_tool.id] if search_tool else None
        persona = upsert_persona(
            user=owner,
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
            document_set_ids=[document_set_id],
            tool_ids=tool_ids,
        )
        persona_id = persona.id
        set_provisioned_ids(db_session, request, persona_id=persona_id)

    # 3. Slack bot config -> persona, making Darwin live in the channel.
    if slack_bot_config_id is None:
        channel_config = _build_channel_config(
            payload, _prioritized_sources(payload["sources"])
        )
        response_type = (
            SlackBotResponseType.QUOTES
            if payload.get("response_type") == "quotes"
            else SlackBotResponseType.CITATIONS
        )
        slack_config = insert_slack_bot_config(
            persona_id=persona_id,
            channel_config=channel_config,
            response_type=response_type,
            db_session=db_session,
        )
        slack_bot_config_id = slack_config.id
        set_provisioned_ids(
            db_session, request, slack_bot_config_id=slack_bot_config_id
        )

    # 4. Mark live + notify. A notification failure must not fail finalize — the
    # request is already COMPLETE (and would otherwise be retried forever).
    update_onboarding_status(db_session, request, OnboardingStatus.COMPLETE)
    logger.info(
        "onboarding %s finalized: persona=%s doc_set=%s config=%s",
        request.id,
        persona_id,
        document_set_id,
        slack_bot_config_id,
    )
    try:
        notify_onboarding_complete(request)
        notify_client_complete(request)
    except Exception:
        logger.exception(
            "onboarding %s finalized but completion notification failed", request.id
        )
    return request


def _source_index_state(
    request: OnboardingRequest, db_session: Session
) -> tuple[bool, list[str], bool]:
    """(all_indexed, failures, in_progress) from each cc_pair's LATEST index
    attempt. all_indexed is True only when every source's latest run succeeded;
    failures lists reasons for any source whose latest run failed; in_progress is
    True when any source is still scraping (NOT_STARTED / IN_PROGRESS) — used to
    move a previously-FAILED request back to INDEXING once it's re-indexing."""
    cc_pair_ids = request.cc_pair_ids or []
    if not cc_pair_ids:
        return False, [], False
    statuses: list[IndexingStatus | None] = []
    failures: list[str] = []
    for cc_id in cc_pair_ids:
        cc = get_connector_credential_pair_from_id(cc_id, db_session)
        if cc is None:
            statuses.append(None)
            continue
        latest = db_session.execute(
            select(IndexAttempt)
            .where(IndexAttempt.connector_id == cc.connector_id)
            .where(IndexAttempt.credential_id == cc.credential_id)
            .order_by(desc(IndexAttempt.time_created))
            .limit(1)
        ).scalar_one_or_none()
        status = latest.status if latest else None
        statuses.append(status)
        if status == IndexingStatus.FAILED:
            reason = (latest.error_msg if latest else None) or "indexing failed"
            failures.append(f"{cc.name}: {reason[:150]}")
    all_indexed = bool(statuses) and all(s == IndexingStatus.SUCCESS for s in statuses)
    in_progress = any(
        s in (IndexingStatus.NOT_STARTED, IndexingStatus.IN_PROGRESS) for s in statuses
    )
    return all_indexed, failures, in_progress


def finalize_ready_onboarding_requests(db_session: Session) -> None:
    """Background poll: for each request still being tracked (INDEXING or FAILED),
    drive its status from the aggregate state of its sources. Ordering matters —
    in-progress WINS over a failure so the requester never sees a scary FAILED
    while work is still happening:

      * all sources succeeded            -> finalize (assistant goes live) -> COMPLETE
      * any source still scraping        -> INDEXING (defer any failure verdict;
                                            a stale FAILED is cleared back to
                                            INDEXING so re-indexing shows progress)
      * settled with >=1 failed source   -> FAILED + notify ONCE

    Recoverable by design: fix + re-index a source and the request climbs back to
    INDEXING and then COMPLETE on later ticks; nothing needs restarting. An
    already-FAILED request that is still fully settled+failing is not re-notified."""
    tracked = list_onboarding_requests(
        db_session, OnboardingStatus.INDEXING
    ) + list_onboarding_requests(db_session, OnboardingStatus.FAILED)
    for request in tracked:
        try:
            all_indexed, failures, in_progress = _source_index_state(
                request, db_session
            )
            if all_indexed:
                finalize_onboarding(request, db_session)
            elif in_progress:
                # Something is still scraping (initial run or a re-index). Show
                # INDEXING and hold off on any failure verdict — a transient
                # failure may still be superseded by an in-flight or retried
                # source. Only flips the row if it isn't already INDEXING (also
                # clears a stale error_msg from a prior FAILED).
                if request.status != OnboardingStatus.INDEXING.value:
                    update_onboarding_status(
                        db_session, request, OnboardingStatus.INDEXING
                    )
            elif failures and request.status != OnboardingStatus.FAILED.value:
                # Everything settled and at least one source failed: flag FAILED +
                # notify ONCE (an already-FAILED request is not re-notified).
                detail = "; ".join(failures)
                update_onboarding_status(
                    db_session, request, OnboardingStatus.FAILED, error_msg=detail
                )
                notify_onboarding_failed(request, detail)
                notify_client_failed(request)
        except Exception:
            logger.exception("onboarding %s finalize check failed", request.id)
            db_session.rollback()
