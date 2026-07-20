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


# INTERNAL ops channel — admin review link + real error details. Default
# #darwin-devs (C07B2V8E99S), a PRIVATE cross-Grid channel addressed by id; the
# bot is a member. Override via env/configmap (id, name, or channel link).
ONBOARDING_NOTIFY_CHANNEL = _resolve_channel(
    os.environ.get("ONBOARDING_NOTIFY_CHANNEL") or "C07B2V8E99S"
)

# CUSTOMER-FACING channel — the requester is @mentioned on submit and every
# lifecycle update is posted as a *reply in that one thread* (never new top-level
# messages, so a large channel isn't spammed). Defaults to #darwin-devs for
# testing; set ONBOARDING_CLIENT_CHANNEL to #help-darwin (C077AFEGCKZ) in prod
# once the bot has been added there.
ONBOARDING_CLIENT_CHANNEL = _resolve_channel(
    os.environ.get("ONBOARDING_CLIENT_CHANNEL") or "C07B2V8E99S"
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


# --- customer-facing thread (#help-darwin) ----------------------------------


def _mention(client: WebClient, email: str | None) -> str:
    """Resolve requester email -> Slack @mention; fall back to the email text
    (e.g. external address not in the workspace). Never raises."""
    if not email:
        return "there"
    try:
        uid = client.users_lookupByEmail(email=email)["user"]["id"]
        return f"<@{uid}>"
    except Exception:
        return email


def _ensure_member(client: WebClient, channel_id: str) -> None:
    """Best-effort join so the bot can post to a public channel. No-op/ignored
    for private channels (already invited) or when the scope is missing."""
    try:
        client.conversations_join(channel=channel_id)
    except Exception:
        pass


def notify_client_submitted(request: OnboardingRequest) -> str | None:
    """Root customer-facing message on submit: @mention the requester + tracking
    link. Returns the message ts to persist so later updates thread under it.
    Returns None (and posts nothing further) on any failure — callers must handle
    a missing ts by skipping replies, never by posting top-level."""
    if not ONBOARDING_CLIENT_CHANNEL:
        return None
    team, channel = _team_and_channel(request)
    try:
        client = WebClient(token=fetch_tokens().bot_token)
        _ensure_member(client, ONBOARDING_CLIENT_CHANNEL)
        who = _mention(client, request.requester_email)
        resp = client.chat_postMessage(
            channel=ONBOARDING_CLIENT_CHANNEL,
            text=(
                f":wave: Hi {who} — your *Darwin onboarding request* for *{team}* "
                f"(#{channel}) has been received. Track its status any time here: "
                f"{WEB_DOMAIN}/onboarding?view=requests\n"
                f"I'll post updates in this thread as it progresses."
            ),
            unfurl_links=False,
        )
        return resp.get("ts")
    except Exception as e:
        logger.warning(
            "failed to post customer onboarding root for request %s: %s",
            request.id,
            e,
        )
        return None


def _client_reply(request: OnboardingRequest, text: str) -> None:
    """Post a threaded reply under the request's root message. Skips entirely if
    there is no stored thread ts — a large customer channel must never receive
    stray top-level messages. Best-effort; never raises."""
    ts = getattr(request, "help_thread_ts", None)
    if not ts or not ONBOARDING_CLIENT_CHANNEL:
        return
    try:
        client = WebClient(token=fetch_tokens().bot_token)
        client.chat_postMessage(
            channel=ONBOARDING_CLIENT_CHANNEL,
            text=text,
            thread_ts=ts,
            unfurl_links=False,
        )
    except Exception as e:
        logger.warning(
            "failed to post customer onboarding reply for request %s: %s",
            request.id,
            e,
        )


def notify_client_approved(request: OnboardingRequest) -> None:
    """Reply: admin reviewed + approved; indexing has started."""
    _client_reply(
        request,
        ":white_check_mark: Your request has been reviewed and *approved* — "
        "Darwin is now indexing your sources. I'll update this thread once it's "
        "live.",
    )


def notify_client_complete(request: OnboardingRequest) -> None:
    """Reply: all sources indexed, assistant live in the channel."""
    _, channel = _team_and_channel(request)
    _client_reply(
        request,
        f":tada: *Darwin is now live in #{channel}!* All your sources are indexed "
        f"and the assistant is ready — just ask it a question in the channel.",
    )


def notify_client_failed(request: OnboardingRequest) -> None:
    """Reply: a source failed; team notified. No internal error details here."""
    _client_reply(
        request,
        ":warning: One of your sources hit a snag while indexing. Our team has "
        "been notified and is looking into it — nothing needed from you; I'll "
        "update this thread with progress.",
    )
