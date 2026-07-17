import re
import time
from collections.abc import Callable
from collections.abc import Generator
from functools import wraps
from typing import Any
from typing import cast
from urllib.parse import urlparse

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.web import SlackResponse

from danswer.connectors.models import BasicExpertInfo
from danswer.utils.logger import setup_logger

logger = setup_logger()

# number of messages we request per page when fetching paginated slack messages
_SLACK_LIMIT = 900

# Mis-stored Slack workspace subdomains -> their correct URL subdomain.
# Historically some connectors were configured with the workspace *display name*
# ("Product") instead of the URL subdomain ("uipath-product"), so their stored
# permalinks point at the dead host `product.slack.com`. Keys are matched case-
# and whitespace-insensitively, so this covers both "Product" and "Product ".
# This is an explicit allow-map on purpose: only listed subdomains are rewritten,
# so genuinely different workspaces (uipath-customer-ops, uipath-marketing, ...)
# and links already on a correct host are left untouched. Add an entry here if a
# new mis-configured workspace surfaces.
_SLACK_SUBDOMAIN_FIXES = {
    "product": "uipath-product",
}

# Captures the subdomain in the `//<sub>.slack.com` portion of a permalink.
# `[^/]*?` is non-greedy and tolerates a stray space ("Product .slack.com").
_SLACK_HOST_RE = re.compile(r"(//)([^/]*?)(\.slack\.com)", re.IGNORECASE)


def get_message_link(
    event: dict[str, Any], workspace: str, channel_id: str | None = None
) -> str:
    channel_id = channel_id or cast(
        str, event["channel"]
    )  # channel must either be present in the event or passed in
    message_ts = cast(str, event["ts"])
    message_ts_without_dot = message_ts.replace(".", "")
    thread_ts = cast(str | None, event.get("thread_ts"))
    return (
        f"https://{workspace}.slack.com/archives/{channel_id}/p{message_ts_without_dot}"
        + (f"?thread_ts={thread_ts}" if thread_ts else "")
    )


def normalize_slack_link(url: str) -> str:
    """Rewrite a mis-stored Slack workspace subdomain to the canonical one.

    Existing indexed docs carry links built from a mis-configured workspace
    ("Product" -> dead `product.slack.com`). Retrieval reads every citation
    link back through here, so chat / search / Slack-bot citations all resolve
    to the real workspace without re-indexing or a data migration. Only the
    subdomains in `_SLACK_SUBDOMAIN_FIXES` are rewritten; a link on the canonical
    host, or on a genuinely different workspace, is returned unchanged."""
    if not url or ".slack.com" not in url.lower():
        return url

    def _fix(match: "re.Match[str]") -> str:
        subdomain = match.group(2).strip().lower()
        canonical = _SLACK_SUBDOMAIN_FIXES.get(subdomain)
        if canonical is not None:
            return f"{match.group(1)}{canonical}{match.group(3)}"
        return match.group(0)

    return _SLACK_HOST_RE.sub(_fix, url, count=1)


def resolve_workspace_subdomain(
    client: WebClient, channel_id: str, message_ts: str, fallback: str
) -> str:
    """Ask Slack for the authoritative workspace subdomain of a channel.

    `chat.getPermalink` returns the real permalink for a message, with the
    correct `<sub>.slack.com` host even under Enterprise Grid (where a single
    bot can see channels that live in different workspaces, each with its own
    URL). We resolve this ONCE per channel and reuse it, so the connector no
    longer depends on a hand-typed `workspace` config being right. Falls back to
    `fallback` (the configured workspace) if the call fails."""
    try:
        resp = client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        permalink = cast(str, resp.get("permalink") or "")
        host = urlparse(permalink).netloc.lower()
        if host.endswith(".slack.com"):
            subdomain = host[: -len(".slack.com")]
            if subdomain:
                return subdomain
    except SlackApiError as e:
        logger.warning(
            "chat.getPermalink failed for channel %s (%s); "
            "falling back to configured workspace '%s'",
            channel_id,
            e.response.get("error"),
            fallback,
        )
    except Exception as e:
        logger.warning(
            "could not resolve workspace subdomain for channel %s: %s", channel_id, e
        )
    return fallback


def make_slack_api_call_logged(
    call: Callable[..., SlackResponse],
) -> Callable[..., SlackResponse]:
    @wraps(call)
    def logged_call(**kwargs: Any) -> SlackResponse:
        logger.debug(f"Making call to Slack API '{call.__name__}' with args '{kwargs}'")
        result = call(**kwargs)
        logger.debug(f"Call to Slack API '{call.__name__}' returned '{result}'")
        return result

    return logged_call


def make_slack_api_call_paginated(
    call: Callable[..., SlackResponse],
) -> Callable[..., Generator[dict[str, Any], None, None]]:
    """Wraps calls to slack API so that they automatically handle pagination"""

    @wraps(call)
    def paginated_call(**kwargs: Any) -> Generator[dict[str, Any], None, None]:
        cursor: str | None = None
        has_more = True
        while has_more:
            response = call(cursor=cursor, limit=_SLACK_LIMIT, **kwargs)
            yield cast(dict[str, Any], response.validate())
            cursor = cast(dict[str, Any], response.get("response_metadata", {})).get(
                "next_cursor", ""
            )
            has_more = bool(cursor)

    return paginated_call


def make_slack_api_rate_limited(
    call: Callable[..., SlackResponse], max_retries: int = 7
) -> Callable[..., SlackResponse]:
    """Wraps calls to slack API so that they automatically handle rate limiting"""

    @wraps(call)
    def rate_limited_call(**kwargs: Any) -> SlackResponse:
        last_exception = None
        for _ in range(max_retries):
            try:
                # Make the API call
                response = call(**kwargs)

                # Check for errors in the response, will raise `SlackApiError`
                # if anything went wrong
                response.validate()
                return response

            except SlackApiError as e:
                last_exception = e
                try:
                    error = e.response["error"]
                except KeyError:
                    error = "unknown error"

                if error == "ratelimited":
                    # Handle rate limiting: get the 'Retry-After' header value and sleep for that duration
                    retry_after = int(e.response.headers["Retry-After"])
                    logger.info(
                        f"Slack call rate limited, retrying after {retry_after} seconds. Exception: {e}"
                    )
                    time.sleep(retry_after)
                    continue
                elif error in ["already_reacted", "no_reaction"]:
                    logger.info("not here already_reacted")
                    # The response isn't used for reactions, this is basically just a pass
                    return e.response
                elif "status" in e.response and e.response["status"] == 503:
                    time.sleep(60)
                    continue
                else:
                    # Raise the error for non-transient errors
                    logger.info(f"Non Transient Exception: {e}")
                    raise

        # If the code reaches this point, all retries have been exhausted
        msg = f"Max retries ({max_retries}) exceeded"
        if last_exception:
            raise Exception(msg) from last_exception
        else:
            raise Exception(msg)

    return rate_limited_call


def expert_info_from_slack_id(
    user_id: str | None,
    client: WebClient,
    user_cache: dict[str, BasicExpertInfo | None],
) -> BasicExpertInfo | None:
    if not user_id:
        return None

    if user_id in user_cache:
        return user_cache[user_id]

    response = make_slack_api_rate_limited(client.users_info)(user=user_id)

    if not response["ok"]:
        user_cache[user_id] = None
        return None

    user: dict = cast(dict[Any, dict], response.data).get("user", {})
    profile = user.get("profile", {})

    expert = BasicExpertInfo(
        display_name=user.get("real_name") or profile.get("display_name"),
        first_name=profile.get("first_name"),
        last_name=profile.get("last_name"),
        email=profile.get("email"),
    )

    user_cache[user_id] = expert

    return expert


class SlackTextCleaner:
    """Utility class to replace user IDs with usernames in a message.
    Handles caching, so the same request is not made multiple times
    for the same user ID"""

    def __init__(self, client: WebClient) -> None:
        self._client = client
        self._id_to_name_map: dict[str, str] = {}

    def _get_slack_name(self, user_id: str) -> str:
        if user_id not in self._id_to_name_map:
            try:
                response = make_slack_api_rate_limited(self._client.users_info)(
                    user=user_id
                )
                # prefer display name if set, since that is what is shown in Slack
                self._id_to_name_map[user_id] = (
                    response["user"]["profile"]["display_name"]
                    or response["user"]["profile"]["real_name"]
                )
            except SlackApiError as e:
                logger.exception(
                    f"Error fetching data for user {user_id}: {e.response['error']}"
                )
                raise

        return self._id_to_name_map[user_id]

    def _replace_user_ids_with_names(self, message: str) -> str:
        # Find user IDs in the message
        user_ids = re.findall("<@(.*?)>", message)

        # Iterate over each user ID found
        for user_id in user_ids:
            try:
                if user_id in self._id_to_name_map:
                    user_name = self._id_to_name_map[user_id]
                else:
                    user_name = self._get_slack_name(user_id)

                # Replace the user ID with the username in the message
                message = message.replace(f"<@{user_id}>", f"@{user_name}")
            except Exception:
                logger.exception(
                    f"Unable to replace user ID with username for user_id '{user_id}'"
                )

        return message

    def index_clean(self, message: str) -> str:
        """During indexing, replace pattern sets that may cause confusion to the model
        Some special patterns are left in as they can provide information
        ie. links that contain format text|link, both the text and the link may be informative
        """
        message = self._replace_user_ids_with_names(message)
        message = self.replace_tags_basic(message)
        message = self.replace_channels_basic(message)
        message = self.replace_special_mentions(message)
        message = self.replace_special_catchall(message)
        return message

    @staticmethod
    def replace_tags_basic(message: str) -> str:
        """Simply replaces all tags with `@<USER_ID>` in order to prevent us from
        tagging users in Slack when we don't want to"""
        # Find user IDs in the message
        user_ids = re.findall("<@(.*?)>", message)
        for user_id in user_ids:
            message = message.replace(f"<@{user_id}>", f"@{user_id}")
        return message

    @staticmethod
    def replace_channels_basic(message: str) -> str:
        """Simply replaces all channel mentions with `#<CHANNEL_ID>` in order
        to make a message work as part of a link"""
        # Find user IDs in the message
        channel_matches = re.findall(r"<#(.*?)\|(.*?)>", message)
        for channel_id, channel_name in channel_matches:
            message = message.replace(
                f"<#{channel_id}|{channel_name}>", f"#{channel_name}"
            )
        return message

    @staticmethod
    def replace_special_mentions(message: str) -> str:
        """Simply replaces @channel, @here, and @everyone so we don't tag
        a bunch of people in Slack when we don't want to"""
        # Find user IDs in the message
        message = message.replace("<!channel>", "@channel")
        message = message.replace("<!here>", "@here")
        message = message.replace("<!everyone>", "@everyone")
        return message

    @staticmethod
    def replace_links(message: str) -> str:
        """Replaces slack links e.g. `<URL>` -> `URL` and `<URL|DISPLAY>` -> `DISPLAY`"""
        # Find user IDs in the message
        possible_link_matches = re.findall(r"<(.*?)>", message)
        for possible_link in possible_link_matches:
            if not possible_link:
                continue
            # Special slack patterns that aren't for links
            if possible_link[0] not in ["#", "@", "!"]:
                link_display = (
                    possible_link
                    if "|" not in possible_link
                    else possible_link.split("|")[1]
                )
                message = message.replace(f"<{possible_link}>", link_display)
        return message

    @staticmethod
    def replace_special_catchall(message: str) -> str:
        """Replaces pattern of <!something|another-thing> with another-thing
        This is added for <!subteam^TEAM-ID|@team-name> but may match other cases as well
        """

        pattern = r"<!([^|]+)\|([^>]+)>"
        return re.sub(pattern, r"\2", message)

    @staticmethod
    def add_zero_width_whitespace_after_tag(message: str) -> str:
        """Add a 0 width whitespace after every @"""
        return message.replace("@", "@\u200B")

    @staticmethod
    def handle_bold_syntax_for_slack(text: str) -> str:
        """Replace instances of '**' with a single '*'"""
        corrected_text = text.replace("**", "*")
        return corrected_text
