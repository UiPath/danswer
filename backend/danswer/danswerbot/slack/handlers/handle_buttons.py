from datetime import datetime
from typing import Any
from typing import cast

from slack_sdk import WebClient
from slack_sdk.models.blocks import SectionBlock
from slack_sdk.models.views import View
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from sqlalchemy.orm import Session

from danswer.configs.constants import SearchFeedbackType
from danswer.configs.danswerbot_configs import DANSWER_FOLLOWUP_EMOJI
from danswer.connectors.slack.utils import make_slack_api_rate_limited
from danswer.danswerbot.slack.blocks import build_follow_up_resolved_blocks
from danswer.danswerbot.slack.blocks import build_sme_verified_blocks
from danswer.danswerbot.slack.blocks import get_document_feedback_blocks
from danswer.danswerbot.slack.config import get_slack_bot_config_for_channel
from danswer.danswerbot.slack.constants import CURATED_RESPONSE_CONFIG_KEY
from danswer.danswerbot.slack.constants import DISLIKE_BLOCK_ACTION_ID
from danswer.danswerbot.slack.constants import ENABLE_CURATED_RESPONSE_KEY
from danswer.danswerbot.slack.constants import FeedbackVisibility
from danswer.danswerbot.slack.constants import LIKE_BLOCK_ACTION_ID
from danswer.danswerbot.slack.constants import SME_VALIDATE_BUTTON_ACTION_ID
from danswer.danswerbot.slack.constants import RESPONSE_MESSAGE_KEY
from danswer.danswerbot.slack.constants import USER_ID_KEY
from danswer.danswerbot.slack.constants import USER_KEY
from danswer.danswerbot.slack.constants import USER_PROFILE_KEY
from danswer.danswerbot.slack.constants import USER_TITLE_FILTER_KEY
from danswer.danswerbot.slack.constants import USER_TITLE_KEY
from danswer.danswerbot.slack.constants import VIEW_DOC_FEEDBACK_ID
from danswer.danswerbot.slack.handlers.handle_message import (
    remove_scheduled_feedback_reminder,
)
from danswer.danswerbot.slack.tools.opsgenie import get_dri_on_call
from danswer.danswerbot.slack.utils import build_feedback_id
from danswer.danswerbot.slack.utils import decompose_action_id
from danswer.danswerbot.slack.utils import fetch_groupids_from_names
from danswer.danswerbot.slack.utils import fetch_userids_from_emails
from danswer.danswerbot.slack.utils import get_channel_name_from_id
from danswer.danswerbot.slack.utils import get_feedback_visibility
from danswer.danswerbot.slack.utils import respond_in_thread
from danswer.danswerbot.slack.utils import update_emote_react
from danswer.db.engine import get_sqlalchemy_engine
from danswer.db.feedback import create_chat_message_feedback
from danswer.db.feedback import create_doc_retrieval_feedback
from danswer.db.feedback import mark_message_sme_verified
from danswer.document_index.document_index_utils import get_both_index_names
from danswer.document_index.factory import get_default_document_index
from danswer.utils.logger import setup_logger

logger_base = setup_logger()


def handle_curated_response(
    slack_bot_config: Any,
    client: SocketModeClient,
    req: SocketModeRequest,
    channel_id: str,
    thread_ts: str,
) -> bool:
    """Handle curated response based on user title filter.

    Returns:
        bool: True if a curated response was sent, else returns False.
    """
    if not slack_bot_config or not slack_bot_config.channel_config:
        return False

    channel_conf = slack_bot_config.channel_config
    curated_response_config = channel_conf.get(CURATED_RESPONSE_CONFIG_KEY, {})

    # Early return if curated response is not enabled
    if not curated_response_config.get(ENABLE_CURATED_RESPONSE_KEY, False):
        return False

    if not curated_response_config.get(RESPONSE_MESSAGE_KEY):
        return False

    user_title_filter = channel_conf.get(USER_TITLE_FILTER_KEY, [])
    sender_id = req.payload.get(USER_KEY, {}).get(USER_ID_KEY)

    if not user_title_filter or not sender_id:
        return False

    try:
        user_info = client.web_client.users_info(user=sender_id)
        user_data = user_info.get(USER_KEY)
        if not user_data:
            return False

        user_profile = user_data.get(USER_PROFILE_KEY, {})
        user_title = user_profile.get(USER_TITLE_KEY, "").lower()

        # Check if user title matches any in the filter list
        if user_title in [title.lower() for title in user_title_filter]:
            response_message = curated_response_config.get(RESPONSE_MESSAGE_KEY)

            respond_in_thread(
                client=client.web_client,
                channel=channel_id,
                text=response_message,
                thread_ts=thread_ts,
                unfurl=False,
            )
            return True
    except Exception as e:
        logger_base.error(f"Failed to check user title for curated response: {str(e)}")

    return False


def handle_doc_feedback_button(
    req: SocketModeRequest,
    client: SocketModeClient,
) -> None:
    if not (actions := req.payload.get("actions")):
        logger_base.error("Missing actions. Unable to build the source feedback view")
        return

    # Extracts the feedback_id coming from the 'source feedback' button
    # and generates a new one for the View, to keep track of the doc info
    query_event_id, doc_id, doc_rank = decompose_action_id(actions[0].get("value"))
    external_id = build_feedback_id(query_event_id, doc_id, doc_rank)

    channel_id = req.payload["container"]["channel_id"]
    thread_ts = req.payload["container"]["thread_ts"]

    data = View(
        type="modal",
        callback_id=VIEW_DOC_FEEDBACK_ID,
        external_id=external_id,
        # We use the private metadata to keep track of the channel id and thread ts
        private_metadata=f"{channel_id}_{thread_ts}",
        title="Give Feedback",
        blocks=[get_document_feedback_blocks()],
        submit="send",
        close="cancel",
    )

    client.web_client.views_open(
        trigger_id=req.payload["trigger_id"], view=data.to_dict()
    )


def handle_slack_feedback(
    feedback_id: str,
    feedback_type: str,
    feedback_msg_reminder: str,
    client: WebClient,
    user_id_to_post_confirmation: str,
    channel_id_to_post_confirmation: str,
    thread_ts_to_post_confirmation: str,
) -> None:
    engine = get_sqlalchemy_engine()

    message_id, doc_id, doc_rank = decompose_action_id(feedback_id)

    with Session(engine) as db_session:
        if feedback_type in [LIKE_BLOCK_ACTION_ID, DISLIKE_BLOCK_ACTION_ID]:
            create_chat_message_feedback(
                is_positive=feedback_type == LIKE_BLOCK_ACTION_ID,
                feedback_text="",
                chat_message_id=message_id,
                user_id=None,  # no "user" for Slack bot for now
                db_session=db_session,
            )
            remove_scheduled_feedback_reminder(
                client=client,
                channel=user_id_to_post_confirmation,
                msg_id=feedback_msg_reminder,
            )
        elif feedback_type in [
            SearchFeedbackType.ENDORSE.value,
            SearchFeedbackType.REJECT.value,
            SearchFeedbackType.HIDE.value,
        ]:
            if doc_id is None or doc_rank is None:
                raise ValueError("Missing information for Document Feedback")

            if feedback_type == SearchFeedbackType.ENDORSE.value:
                feedback = SearchFeedbackType.ENDORSE
            elif feedback_type == SearchFeedbackType.REJECT.value:
                feedback = SearchFeedbackType.REJECT
            else:
                feedback = SearchFeedbackType.HIDE

            curr_ind_name, sec_ind_name = get_both_index_names(db_session)
            document_index = get_default_document_index(
                primary_index_name=curr_ind_name, secondary_index_name=sec_ind_name
            )

            create_doc_retrieval_feedback(
                message_id=message_id,
                document_id=doc_id,
                document_rank=doc_rank,
                document_index=document_index,
                db_session=db_session,
                clicked=False,  # Not tracking this for Slack
                feedback=feedback,
            )
        else:
            logger_base.error(f"Feedback type '{feedback_type}' not supported")

    if get_feedback_visibility() == FeedbackVisibility.PRIVATE or feedback_type not in [
        LIKE_BLOCK_ACTION_ID,
        DISLIKE_BLOCK_ACTION_ID,
    ]:
        client.chat_postEphemeral(
            channel=channel_id_to_post_confirmation,
            user=user_id_to_post_confirmation,
            thread_ts=thread_ts_to_post_confirmation,
            text="Thanks for your feedback!",
        )
    else:
        feedback_response_txt = (
            "liked" if feedback_type == LIKE_BLOCK_ACTION_ID else "disliked"
        )

        if get_feedback_visibility() == FeedbackVisibility.ANONYMOUS:
            msg = f"A user has {feedback_response_txt} the AI Answer"
        else:
            msg = f"<@{user_id_to_post_confirmation}> has {feedback_response_txt} the AI Answer"

        respond_in_thread(
            client=client,
            channel=channel_id_to_post_confirmation,
            text=msg,
            thread_ts=thread_ts_to_post_confirmation,
            unfurl=False,
        )


def _sme_ephemeral(
    web_client: WebClient, channel: str, user: str, text: str
) -> None:
    try:
        make_slack_api_rate_limited(web_client.chat_postEphemeral)(
            channel=channel, user=user, text=text
        )
    except Exception:
        logger_base.exception("Failed to post SME ephemeral message")


def _is_sme_action_block(block: dict[str, Any]) -> bool:
    if block.get("type") != "actions":
        return False
    return any(
        el.get("action_id") == SME_VALIDATE_BUTTON_ACTION_ID
        for el in block.get("elements", [])
    )


def handle_sme_validate_button(
    req: SocketModeRequest,
    client: SocketModeClient,
) -> None:
    """'Awaiting SME Review' button. Only members of the channel's
    configured Slack user group may verify (checked live, so leavers are handled).
    On success the red button is swapped for a green 'Verified by an SME' badge
    naming the verifier. Non-members get a private rejection; re-clicks are no-ops."""
    payload = req.payload
    user_id = payload["user"]["id"]
    container = payload.get("container", {})
    channel_id = container.get("channel_id") or payload.get("channel", {}).get("id")
    message_ts = container.get("message_ts")
    blocks: list[dict[str, Any]] = payload.get("message", {}).get("blocks", [])
    web = client.web_client

    if not channel_id or not message_ts:
        return

    # Idempotent: if already verified (the green/primary SME button is present), stop.
    already_verified = any(
        el.get("action_id") == SME_VALIDATE_BUTTON_ACTION_ID
        and el.get("style") == "primary"
        for b in blocks
        if b.get("type") == "actions"
        for el in b.get("elements", [])
    )
    if already_verified:
        _sme_ephemeral(web, channel_id, user_id, "This answer is already verified.")
        return

    # Resolve the channel's SME user group from its config.
    with Session(get_sqlalchemy_engine()) as db_session:
        channel_name, _ = get_channel_name_from_id(
            client=web, channel_id=channel_id
        )
        cfg = get_slack_bot_config_for_channel(
            channel_name=channel_name, db_session=db_session
        )
    channel_conf = cfg.channel_config if cfg else None
    sme_group_name = channel_conf.get("sme_group_name") if channel_conf else None
    if (
        not channel_conf
        or not channel_conf.get("enable_sme_validation")
        or not sme_group_name
    ):
        _sme_ephemeral(
            web,
            channel_id,
            user_id,
            "SME verification isn't configured for this channel.",
        )
        return

    # `sme_group_name` is a comma-separated list of group names/@handles; a clicker
    # who belongs to ANY of them may verify.
    sme_group_names = [n.strip() for n in sme_group_name.split(",") if n.strip()]
    if not sme_group_names:
        _sme_ephemeral(
            web,
            channel_id,
            user_id,
            "SME verification isn't configured for this channel.",
        )
        return

    # Resolve the configured group names/@handles -> ids (live), so config stays
    # human-friendly and renames of members are irrelevant.
    group_ids, failed_names = fetch_groupids_from_names(sme_group_names, web)
    if failed_names:
        logger_base.error("SME group(s) not found in workspace: %r", failed_names)
    if not group_ids:
        _sme_ephemeral(
            web,
            channel_id,
            user_id,
            "The configured SME group couldn't be found — please check the channel setup.",
        )
        return

    # AUTHORIZE (trusted side): clicker must be a LIVE member of at least one SME
    # user group. Fetch members per group and union them; a per-group fetch error
    # is skipped (other groups can still authorize).
    members: set[str] = set()
    fetch_errors = 0
    for gid in group_ids:
        try:
            members.update(
                make_slack_api_rate_limited(web.usergroups_users_list)(usergroup=gid)[
                    "users"
                ]
            )
        except Exception:
            fetch_errors += 1
            logger_base.exception("Failed to fetch SME user group %s", gid)
    if not members and fetch_errors:
        _sme_ephemeral(
            web,
            channel_id,
            user_id,
            "Couldn't check your SME membership just now — please try again.",
        )
        return
    if user_id not in members:
        _sme_ephemeral(
            web,
            channel_id,
            user_id,
            "Only members of the SME group can verify answers.",
        )
        return

    # Recover the message_id encoded in the SME block's block_id (best effort).
    message_id: int | None = None
    for b in blocks:
        if _is_sme_action_block(b) and b.get("block_id"):
            try:
                message_id, _, _ = decompose_action_id(b["block_id"])
            except ValueError:
                message_id = None
            break

    # Swap the red button for the green verified state; keep everything else.
    when = datetime.now().strftime("%b %d, %Y")
    verified_blocks = [
        blk.to_dict()
        for blk in build_sme_verified_blocks(
            validator_name=f"<@{user_id}>", when=when, message_id=message_id
        )
    ]
    new_blocks = [b for b in blocks if not _is_sme_action_block(b)] + verified_blocks

    try:
        make_slack_api_rate_limited(web.chat_update)(
            channel=channel_id,
            ts=message_ts,
            blocks=new_blocks,
            text="This answer has been verified by an SME.",
        )
    except Exception:
        logger_base.exception("Failed to update message with SME verification")
        _sme_ephemeral(
            web, channel_id, user_id, "Couldn't mark this verified — please retry."
        )
        return

    # Persist the verification (extends chat_feedback) for reporting. Best-effort:
    # a storage hiccup must not undo the visible badge. Record the verifier's email
    # when resolvable (more portable than a Slack id), else the Slack id.
    if message_id is not None:
        verifier = user_id
        try:
            info = web.users_info(user=user_id)
            verifier = (
                info.get("user", {}).get("profile", {}).get("email") or user_id
            )
        except Exception:
            pass
        try:
            with Session(get_sqlalchemy_engine()) as db_session:
                mark_message_sme_verified(
                    chat_message_id=message_id,
                    verified_by=verifier,
                    db_session=db_session,
                )
        except Exception:
            logger_base.exception("Failed to persist SME verification")

    logger_base.info(
        "SME verification: channel=%s ts=%s message_id=%s verified_by=%s",
        channel_id,
        message_ts,
        message_id,
        user_id,
    )


def handle_followup_button(
    req: SocketModeRequest,
    client: SocketModeClient,
) -> None:
    action_id = None
    if actions := req.payload.get("actions"):
        action = cast(dict[str, Any], actions[0])
        action_id = cast(str, action.get("block_id"))

    channel_id = req.payload["container"]["channel_id"]
    thread_ts = req.payload["container"]["thread_ts"]

    update_emote_react(
        emoji=DANSWER_FOLLOWUP_EMOJI,
        channel=channel_id,
        message_ts=thread_ts,
        remove=False,
        client=client.web_client,
    )

    tag_ids: list[str] = []
    group_ids: list[str] = []
    with Session(get_sqlalchemy_engine()) as db_session:
        channel_name, is_dm = get_channel_name_from_id(
            client=client.web_client, channel_id=channel_id
        )
        slack_bot_config = get_slack_bot_config_for_channel(
            channel_name=channel_name, db_session=db_session
        )
        if slack_bot_config:
            tag_names = slack_bot_config.channel_config.get("follow_up_tags")
            remaining = None
            if tag_names:
                tag_ids, remaining = fetch_userids_from_emails(
                    tag_names, client.web_client
                )
            if remaining:
                group_ids, _ = fetch_groupids_from_names(remaining, client.web_client)

            # Get the DRI on call for this channel
            dri_name = get_dri_on_call(slack_bot_config.channel_config)
            if dri_name:
                dri_ids, _ = fetch_userids_from_emails([dri_name], client.web_client)
                if dri_ids:
                    tag_ids.extend(dri_ids)

    # Pass the message_id (decoded from the followup button's block_id)
    # so the "Mark Resolved" button can attribute its feedback row to the
    # right chat_message.
    resolved_message_id: int | None = None
    if action_id is not None:
        try:
            resolved_message_id, _, _ = decompose_action_id(action_id)
        except ValueError:
            resolved_message_id = None
    blocks = build_follow_up_resolved_blocks(
        tag_ids=tag_ids, group_ids=group_ids, message_id=resolved_message_id
    )

    # Check for curated response based on user title
    curated_response_sent = handle_curated_response(
        slack_bot_config=slack_bot_config,
        client=client,
        req=req,
        channel_id=channel_id,
        thread_ts=thread_ts,
    )

    # Only send the default response if no curated response was sent
    if not curated_response_sent:
        respond_in_thread(
            client=client.web_client,
            channel=channel_id,
            text="Received your request for more help",
            blocks=blocks,
            thread_ts=thread_ts,
            unfurl=False,
        )

    if action_id is not None:
        message_id, _, _ = decompose_action_id(action_id)

        create_chat_message_feedback(
            is_positive=None,
            feedback_text="",
            chat_message_id=message_id,
            user_id=None,  # no "user" for Slack bot for now
            db_session=db_session,
            required_followup=True,
        )


def get_clicker_name(
    req: SocketModeRequest,
    client: SocketModeClient,
) -> str:
    clicker_name = req.payload.get("user", {}).get("name", "Someone")
    clicker_real_name = None
    try:
        clicker = client.web_client.users_info(user=req.payload["user"]["id"])
        clicker_real_name = (
            cast(dict, clicker.data).get("user", {}).get("profile", {}).get("real_name")
        )
    except Exception:
        # Likely a scope issue
        pass

    if clicker_real_name:
        clicker_name = clicker_real_name

    return clicker_name


def handle_followup_resolved_button(
    req: SocketModeRequest,
    client: SocketModeClient,
    immediate: bool = False,
) -> None:
    channel_id = req.payload["container"]["channel_id"]
    message_ts = req.payload["container"]["message_ts"]
    thread_ts = req.payload["container"]["thread_ts"]

    clicker_name = get_clicker_name(req, client)

    # Record a chat_feedback row marking this message as resolved so the
    # NPS / analytics pipeline can count "resolved" alongside "like" as
    # a positive signal. Best-effort: if the action_id / block_id doesn't
    # carry a message_id (e.g. older button payloads from before this
    # change), we just log and move on rather than blocking the UX.
    action_block_id: str | None = None
    if actions := req.payload.get("actions"):
        action = cast(dict[str, Any], actions[0])
        action_block_id = cast(str | None, action.get("block_id"))
    if action_block_id:
        try:
            resolved_message_id, _, _ = decompose_action_id(action_block_id)
            with Session(get_sqlalchemy_engine()) as db_session:
                create_chat_message_feedback(
                    is_positive=None,
                    feedback_text="",
                    chat_message_id=resolved_message_id,
                    user_id=None,  # no "user" for Slack bot for now
                    db_session=db_session,
                    predefined_feedback="resolved",
                )
        except (ValueError, Exception) as e:  # noqa: BLE001 — best effort
            logger_base.warning(
                f"Could not record 'resolved' feedback (block_id="
                f"{action_block_id!r}): {e}"
            )
    else:
        logger_base.info(
            "Resolved button clicked but the ActionsBlock had no block_id; "
            "feedback row not recorded. Older message — expected to phase "
            "out as new bot replies use the updated build_follow_up_block."
        )

    update_emote_react(
        emoji=DANSWER_FOLLOWUP_EMOJI,
        channel=channel_id,
        message_ts=thread_ts,
        remove=True,
        client=client.web_client,
    )

    # Delete the message with the option to mark resolved
    if not immediate:
        slack_call = make_slack_api_rate_limited(client.web_client.chat_delete)
        response = slack_call(
            channel=channel_id,
            ts=message_ts,
        )

        if not response.get("ok"):
            logger_base.error("Unable to delete message for resolved")

    if immediate:
        msg_text = f"{clicker_name} has marked this question as resolved!"
    else:
        msg_text = (
            f"{clicker_name} has marked this question as resolved! "
            f'\n\n You can always click the "I need more help button" to let the team '
            f"know that your problem still needs attention."
        )

    resolved_block = SectionBlock(text=msg_text)

    respond_in_thread(
        client=client.web_client,
        channel=channel_id,
        text="Your request for help as been addressed!",
        blocks=[resolved_block],
        thread_ts=thread_ts,
        unfurl=False,
    )
