"""Slack notifications for the onboarding workflow.

Best-effort: a Slack failure must never break the request itself, so every send
is wrapped and only logged on error."""
import os

from slack_sdk import WebClient

from danswer.configs.app_configs import WEB_DOMAIN
from danswer.danswerbot.slack.tokens import fetch_tokens
from danswer.db.models import OnboardingRequest
from danswer.utils.logger import setup_logger

logger = setup_logger()

# Channel to notify when a new onboarding request is submitted. Accepts a channel
# name (the bot must be a member) or a channel id; override via env / configmap.
ONBOARDING_NOTIFY_CHANNEL = os.environ.get("ONBOARDING_NOTIFY_CHANNEL") or "darwin-devs"


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
