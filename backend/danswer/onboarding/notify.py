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


def _sources_summary(payload: dict) -> str:
    sources = payload.get("sources") or []
    parts = [f"{s.get('type')}: {s.get('label') or s.get('value')}" for s in sources]
    return ", ".join(parts) if parts else "none"


def notify_onboarding_submitted(request: OnboardingRequest) -> None:
    """Post a heads-up to the ops channel that a new request needs review."""
    if not ONBOARDING_NOTIFY_CHANNEL:
        return
    payload = request.payload or {}
    team = payload.get("team_name") or "?"
    channel = (payload.get("channel") or {}).get("channel_name") or "?"
    review_url = f"{WEB_DOMAIN}/admin/onboarding"
    text = (
        f":inbox_tray: *New Darwin onboarding request* from "
        f"*{request.requester_email}*\n"
        f"• Team: *{team}*  →  #{channel}\n"
        f"• Sources: {_sources_summary(payload)}\n"
        f"Review & approve: {review_url}"
    )
    try:
        client = WebClient(token=fetch_tokens().bot_token)
        client.chat_postMessage(
            channel=ONBOARDING_NOTIFY_CHANNEL, text=text, unfurl_links=False
        )
    except Exception as e:
        logger.warning(
            "failed to notify '%s' of onboarding request %s: %s",
            ONBOARDING_NOTIFY_CHANNEL,
            request.id,
            e,
        )
