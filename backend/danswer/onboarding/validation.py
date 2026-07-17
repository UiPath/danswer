"""Inline validation for the self-serve onboarding form.

Every source/handle a requester enters is validated against the live system
before submission: Slack channels + user-groups via the bot's Slack client,
Confluence spaces via an existing Confluence connector's credentials, and
docs.uipath.com roots via the web-connector's version logic. Each validator is
resilient — it distinguishes "invalid" from "couldn't check right now"."""
import re
from urllib.parse import urlparse

from pydantic import BaseModel
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from sqlalchemy import select
from sqlalchemy.orm import Session

from danswer.configs.app_configs import GITHUB_CONNECTOR_BASE_URL
from danswer.configs.constants import DocumentSource
from danswer.connectors.confluence.connector import extract_confluence_keys_from_url
from danswer.connectors.web.connector import _uipath_product_prefix
from danswer.danswerbot.slack.tokens import fetch_tokens
from danswer.danswerbot.slack.utils import fetch_groupids_from_names
from danswer.db.models import Connector
from danswer.db.models import Credential
from danswer.utils.logger import setup_logger

logger = setup_logger()

# Slack has no name->channel lookup API. On a large Enterprise Grid org a bare
# name can be thousands of channels deep, so instead of enumerating everything we
# search only the channels the bot is a MEMBER of (users.conversations — a small
# set), which is also the real precondition for the bot to operate in a channel.
_MEMBER_CHANNEL_PAGES = 6  # ~6k channels the bot is in — plenty
_MENTION_RE = re.compile(r"<#(C[A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL_ID_RE = re.compile(r"^C[A-Z0-9]{6,}$")


class ValidationResult(BaseModel):
    valid: bool
    message: str
    # Resolved canonical values (e.g. channel id/name, wiki base) when valid.
    resolved: dict = {}


def _bot_client() -> WebClient:
    return WebClient(token=fetch_tokens().bot_token)


def _find_channel_by_name(
    method: object, name: str, max_pages: int, **kwargs: object
) -> dict | None:
    """Paginate a Slack list endpoint (conversations_list / users_conversations)
    looking for an exact channel-name match."""
    cursor: str | None = None
    for _ in range(max_pages):
        resp = method(limit=1000, cursor=cursor, **kwargs)  # type: ignore[operator]
        for ch in resp.get("channels", []):
            if ch.get("name", "").lower() == name:
                return ch
        meta = resp.get("response_metadata")
        cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
        if not cursor:
            break
    return None


def validate_slack_channel(value: str) -> ValidationResult:
    """Accepts a #mention, a channel id, or a bare name. Resolves to the real
    channel and confirms the bot can see it."""
    raw = (value or "").strip()
    if not raw:
        return ValidationResult(valid=False, message="Enter a channel")

    mention = _MENTION_RE.search(raw)
    channel_id = (
        mention.group(1) if mention else (raw if _CHANNEL_ID_RE.match(raw) else None)
    )
    try:
        client = _bot_client()
    except Exception as e:
        logger.warning("slack client unavailable for validation: %s", e)
        return ValidationResult(valid=False, message="Couldn't reach Slack to verify")

    if channel_id:
        try:
            ch = client.conversations_info(channel=channel_id)["channel"]
            return ValidationResult(
                valid=True,
                message=f"#{ch['name']}",
                resolved={"channel_id": ch["id"], "channel_name": ch["name"]},
            )
        except SlackApiError as e:
            return ValidationResult(
                valid=False,
                message=f"Channel not found or bot lacks access ({e.response.get('error')})",
            )

    # Bare name. Slack has no name->channel lookup API, and every channel-read
    # endpoint (conversations_info/history/replies) needs the ID — so there's no
    # cheap way to confirm a channel the bot isn't in yet, and enumerating the
    # whole org (tens of thousands of channels on Enterprise Grid) is too costly.
    # Crucially, the bot is only added to the channel DURING onboarding, so a
    # brand-new channel legitimately isn't a member yet. We therefore never block
    # on existence: we do a cheap membership check purely as a positive signal
    # (and to resolve the real id/name when we can), and otherwise accept the
    # name — provisioning uses the name, and the connector auto-joins on index.
    name = raw.lstrip("#").lower()
    try:
        ch = _find_channel_by_name(
            client.users_conversations,
            name,
            _MEMBER_CHANNEL_PAGES,
            types="public_channel,private_channel",
        )
    except SlackApiError as e:
        logger.warning(
            "slack membership check failed for %s: %s", name, e.response.get("error")
        )
        ch = None
    if ch:
        return ValidationResult(
            valid=True,
            message=f"#{ch['name']} · the bot is already in this channel",
            resolved={"channel_id": ch["id"], "channel_name": ch["name"]},
        )
    return ValidationResult(
        valid=True,
        message=f"#{name} · the bot will be added to this channel during onboarding",
        resolved={"channel_name": name},
    )


def validate_slack_group(name: str) -> ValidationResult:
    """A Slack user-group / team name (used for SME groups + oncall)."""
    raw = (name or "").strip().lstrip("@")
    if not raw:
        return ValidationResult(valid=False, message="Enter a group name")
    try:
        client = _bot_client()
        group_ids, failed = fetch_groupids_from_names([raw], client)
    except Exception as e:
        logger.warning("slack group validation failed: %s", e)
        return ValidationResult(valid=False, message="Couldn't reach Slack to verify")
    if group_ids:
        return ValidationResult(
            valid=True, message=f"@{raw}", resolved={"group_id": group_ids[0]}
        )
    return ValidationResult(valid=False, message=f"No Slack user group named '{raw}'")


def _first_confluence_credential(db_session: Session) -> dict | None:
    """Reuse an existing Confluence connector's creds for inline space checks."""
    for cred in db_session.execute(select(Credential)).scalars():
        cj = cred.credential_json or {}
        if cj.get("confluence_access_token") and cj.get("confluence_username"):
            return cj
    return None


def _allowed_confluence_hosts(db_session: Session) -> set[str]:
    """Hosts of existing Confluence connectors — the ONLY hosts we'll send our
    stored Confluence token to (SSRF / credential-leak guard: never let a
    user-supplied URL point the authenticated client at an arbitrary host)."""
    hosts: set[str] = set()
    for connector in (
        db_session.execute(
            select(Connector).where(Connector.source == DocumentSource.CONFLUENCE)
        )
        .scalars()
        .all()
    ):
        wiki_url = (connector.connector_specific_config or {}).get("wiki_page_url")
        netloc = urlparse(wiki_url).netloc.lower() if wiki_url else ""
        if netloc:
            hosts.add(netloc)
    return hosts


def validate_confluence_url(url: str, db_session: Session) -> ValidationResult:
    """Parse the wiki URL and confirm the space exists, using an existing
    Confluence connector's credentials — but ONLY if the URL's host matches an
    existing Confluence connector (never send creds to an arbitrary host)."""
    raw = (url or "").strip()
    if not raw:
        return ValidationResult(valid=False, message="Enter a Confluence URL")
    try:
        wiki_base, space, _page_id, is_cloud = extract_confluence_keys_from_url(raw)
    except ValueError:
        return ValidationResult(valid=False, message="Not a valid Confluence wiki URL")

    # SSRF / credential-leak guard: only proceed against a known Confluence host.
    host = urlparse(wiki_base).netloc.lower()
    allowed_hosts = _allowed_confluence_hosts(db_session)
    if allowed_hosts and host not in allowed_hosts:
        return ValidationResult(
            valid=False,
            message=f"Confluence host not allowed — must be one of {sorted(allowed_hosts)}",
        )

    creds = _first_confluence_credential(db_session)
    # No creds, or no known Confluence host to vet against -> parse-only (never
    # send the token to an unvetted host).
    if creds is None or not allowed_hosts:
        return ValidationResult(
            valid=True,
            message=f"Space '{space}' (existence unverified)",
            resolved={"wiki_base": wiki_base, "space": space, "is_cloud": is_cloud},
        )
    try:
        from atlassian import Confluence  # type: ignore[import-untyped]

        client = Confluence(
            url=wiki_base,
            username=creds["confluence_username"],
            password=creds["confluence_access_token"],
            cloud=is_cloud,
        )
        client.get_space(space)
    except Exception as e:
        logger.info("confluence space validation failed for %s: %s", space, e)
        return ValidationResult(
            valid=False, message=f"Space '{space}' not found or inaccessible"
        )
    return ValidationResult(
        valid=True,
        message=f"Space '{space}'",
        resolved={"wiki_base": wiki_base, "space": space, "is_cloud": is_cloud},
    )


_GITHUB_HOSTS = {"github.com", "www.github.com"}


def _first_github_token(db_session: Session) -> str | None:
    """Reuse an existing GitHub connector's token for the access check (the
    requester never supplies one)."""
    for cred in db_session.execute(select(Credential)).scalars():
        tok = (cred.credential_json or {}).get("github_access_token")
        if tok:
            return tok
    return None


def validate_github_repo(url: str, db_session: Session) -> ValidationResult:
    """Confirm a github.com repo URL is reachable with the stored GitHub token —
    i.e. the credential that provisioning will reuse can actually scrape it.

    The token is only ever sent to the GitHub API (github.com or the configured
    enterprise base URL), never to the URL's host. Errors are reported generically
    and logged by exception type only, so a token or raw API payload can't leak
    into a message or the logs."""
    raw = (url or "").strip()
    if not raw:
        return ValidationResult(valid=False, message="Enter a GitHub repo URL")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in _GITHUB_HOSTS:
        return ValidationResult(valid=False, message="Must be a github.com repo URL")
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        return ValidationResult(
            valid=False, message="URL must be github.com/<owner>/<repo>"
        )
    owner, repo = parts[0], parts[1].removesuffix(".git")
    full_name = f"{owner}/{repo}"

    token = _first_github_token(db_session)
    if token is None:
        return ValidationResult(
            valid=True,
            message=f"{full_name} (access unverified — no GitHub credential on file)",
            resolved={"repo_owner": owner, "repo_name": repo},
        )
    try:
        from github import Github  # type: ignore[import-untyped]

        client = (
            Github(token, base_url=GITHUB_CONNECTOR_BASE_URL)
            if GITHUB_CONNECTOR_BASE_URL
            else Github(token)
        )
        client.get_repo(full_name)  # raises if the token can't see the repo
    except Exception as e:
        # Log the exception TYPE only — never str(e), which can echo the token
        # or API payload.
        logger.info(
            "github repo access check failed for %s: %s", full_name, type(e).__name__
        )
        return ValidationResult(
            valid=False,
            message=f"Repo '{full_name}' not found or the GitHub app lacks access",
        )
    return ValidationResult(
        valid=True,
        message=full_name,
        resolved={"repo_owner": owner, "repo_name": repo},
    )


def _first_jira_credential(db_session: Session) -> dict | None:
    """Reuse an existing Jira connector's credential for the filter check."""
    for cred in db_session.execute(select(Credential)).scalars():
        cj = cred.credential_json or {}
        if cj.get("jira_api_token"):
            return cj
    return None


def jira_base_url(db_session: Session) -> str | None:
    """Base URL of an existing Jira connector (onboarding reuses it — the
    requester supplies only the filter, never the host/creds)."""
    for connector in (
        db_session.execute(
            select(Connector).where(Connector.source == DocumentSource.JIRA)
        )
        .scalars()
        .all()
    ):
        base = (connector.connector_specific_config or {}).get("jira_base_url")
        if base:
            return base
    return None


def validate_jira_filter(jql: str, db_session: Session) -> ValidationResult:
    """Confirm a Jira JQL filter is valid and reachable with the stored Jira
    credential (the same one provisioning will reuse).

    The credential is only ever sent to the existing Jira connector's base URL,
    never a user-supplied host. Errors are reported generically and logged by
    exception type only, so a token or JQL-echoing API payload can't leak."""
    raw = (jql or "").strip()
    if not raw:
        return ValidationResult(valid=False, message="Enter a Jira filter (JQL)")

    base = jira_base_url(db_session)
    creds = _first_jira_credential(db_session)
    if base is None or creds is None:
        return ValidationResult(
            valid=True,
            message="Filter unverified — no Jira connector/credential on file",
            resolved={"jira_filter": raw},
        )
    try:
        from jira import JIRA  # type: ignore[import-untyped]

        token = creds["jira_api_token"]
        if creds.get("jira_user_email"):
            client = JIRA(basic_auth=(creds["jira_user_email"], token), server=base)
        else:
            client = JIRA(token_auth=token, server=base)
        # maxResults=1 keeps this cheap; an invalid JQL or access error raises.
        client.search_issues(raw, maxResults=1)
    except Exception as e:
        logger.info("jira filter validation failed: %s", type(e).__name__)
        return ValidationResult(
            valid=False, message="Invalid Jira filter (JQL) or no access"
        )
    return ValidationResult(
        valid=True, message="Jira filter OK", resolved={"jira_filter": raw}
    )


def validate_docs_url(url: str) -> ValidationResult:
    """A docs.uipath.com root URL. We normalize to the product root (version /
    'latest' segment stripped) since the web connector auto-crawls all versions."""
    raw = (url or "").strip()
    if not raw:
        return ValidationResult(valid=False, message="Enter a docs URL")
    if not re.match(r"^https?://docs\.uipath\.com/", raw):
        return ValidationResult(valid=False, message="Must be a docs.uipath.com URL")
    from urllib.parse import urlparse

    parsed = urlparse(raw)
    product_prefix = _uipath_product_prefix(parsed.path)
    if not product_prefix.strip("/"):
        return ValidationResult(valid=False, message="URL must include a product path")
    root = f"{parsed.scheme}://{parsed.netloc}{product_prefix}"
    return ValidationResult(
        valid=True,
        message=f"Will crawl all versions under {root}",
        resolved={"root_url": root},
    )
