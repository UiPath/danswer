"""Slack notifications for the onboarding workflow.

Best-effort: a Slack failure must never break the request itself, so every send
is wrapped and only logged on error."""
import os
import re

from slack_sdk import WebClient

from danswer.configs.app_configs import WEB_DOMAIN
from danswer.danswerbot.slack.tokens import fetch_tokens
from danswer.db.models import OnboardingRequest
from danswer.utils.logger import setup_logger

logger = setup_logger()

_ARCHIVES_RE = re.compile(r"/archives/(C[A-Z0-9]+)")


def _resolve_channel(value: str) -> str:
    """Accept a channel id, a bare name, or a pasted channel link (id parsed out)."""
    m = _ARCHIVES_RE.search(value or "")
    return m.group(1) if m else (value or "").strip()


# Channel to notify when a new onboarding request is submitted, addressed by ID.
# Default is #darwin-devs (C07B2V8E99S) — a PRIVATE channel in another Grid
# workspace, so it must be addressed by id, not name. The DanswerBot app token is
# a member. Override via env/configmap (an id, a name, or a channel link).
ONBOARDING_NOTIFY_CHANNEL = _resolve_channel(
    os.environ.get("ONBOARDING_NOTIFY_CHANNEL") or "C07B2V8E99S"
)


def _post(text: str, request_id: int | None = None) -> None:
    """Post to the ops channel. Best-effort — never raises."""
    if not ONBOARDING_NOTIFY_CHANNEL:
        return
    try:
        client = WebClient(token=fetch_tokens().bot_token)
        client.chat_postMessage(
            channel=ONBOARDING_NOTIFY_CHANNEL, text=text, unfurl_links=False
        )
    except Exception as e:
        logger.warning(
            "failed to notify '%s' of onboarding request %s: %s",
            ONBOARDING_NOTIFY_CHANNEL,
            request_id,
            e,
        )


def _team_and_channel(request: OnboardingRequest) -> tuple[str, str]:
    payload = request.payload or {}
    team = payload.get("team_name") or "?"
    channel = (payload.get("channel") or {}).get("channel_name") or "?"
    return team, channel


def notify_onboarding_submitted(request: OnboardingRequest) -> None:
    """A new request was submitted and needs admin review."""
    team, channel = _team_and_channel(request)
    _post(
        f":inbox_tray: *New Darwin onboarding request* from "
        f"*{request.requester_email}*\n"
        f"• Team: *{team}*  →  #{channel}\n"
        f"Review & approve: {WEB_DOMAIN}/admin/onboarding/{request.id}",
        request.id,
    )


def notify_onboarding_complete(request: OnboardingRequest) -> None:
    """All sources scraped, assistant wired up, Darwin live in the channel."""
    team, channel = _team_and_channel(request)
    _post(
        f":white_check_mark: *Darwin is now live in #{channel}* for *{team}* — "
        f"all sources scraped, the assistant is wired to the new document set.\n"
        f"Status: {WEB_DOMAIN}/admin/onboarding",
        request.id,
    )


def notify_onboarding_failed(request: OnboardingRequest, detail: str) -> None:
    """One or more sources failed to scrape (or provisioning errored)."""
    team, channel = _team_and_channel(request)
    _post(
        f":x: *Darwin onboarding failed for {team}* (#{channel}) — {detail}\n"
        f"Details: {WEB_DOMAIN}/admin/onboarding",
        request.id,
    )
