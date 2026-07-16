from typing import Any

from slack_sdk import WebClient
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import NoResultFound

from danswer.danswerbot.slack.handlers.handle_thread_summary import (
    handle_thread_summary,
)
from danswer.db.engine import get_sqlalchemy_engine
from danswer.db.persona import fetch_persona_by_id
from danswer.db.persona import get_personas
from danswer.db.users import add_slack_persona_for_user
from danswer.db.users import add_user_slack_persona
from danswer.db.users import fetch_user_slack_persona
from danswer.db.users import get_user_by_email
from danswer.utils.logger import setup_logger

logger = setup_logger()


def handle_modal_submission(client: WebClient, body: dict[str, Any]) -> None:
    """Handle the submission of the persona selection modal"""
    try:
        # Get the selected persona ID from the modal submission
        selected_persona_id = body["view"]["state"]["values"]["persona_selection"][
            "select_persona"
        ]["selected_option"]["value"]

        user_id = body["user"]["id"]

        channel_id = body["view"]["private_metadata"]

        with Session(get_sqlalchemy_engine()) as db_session:
            try:
                persona = fetch_persona_by_id(
                    db_session=db_session, persona_id=selected_persona_id
                )

                if persona is None:
                    return {
                        "response_action": "errors",
                        "errors": {"persona_selection": "Persona not found."},
                    }

                # ACL: the modal payload is user-controlled, so re-check that the
                # selected persona is one this Slack user can access before saving.
                # Resolve the Slack user -> Danswer user; known users get public +
                # shared personas, unknown senders get public only.
                # Fail-open: any hiccup resolving the user falls back to the
                # public-persona set (never blocks a legitimate global pick).
                acl_user = None
                try:
                    acl_email = (
                        client.users_info(user=user_id)
                        .data["user"]["profile"]  # type: ignore
                        .get("email")
                    )
                    if acl_email:
                        acl_user = get_user_by_email(
                            email=acl_email, db_session=db_session
                        )
                except Exception:
                    logger.warning(
                        "Unable to resolve Slack user for persona ACL; "
                        "falling back to public personas"
                    )
                accessible = get_personas(
                    user_id=acl_user.id if acl_user else None,
                    db_session=db_session,
                    include_default=True,
                )
                if acl_user is None:
                    accessible = [p for p in accessible if p.is_public]
                if persona.id not in {p.id for p in accessible}:
                    logger.warning(
                        f"Slack user {user_id} tried to select inaccessible "
                        f"persona {persona.id}"
                    )
                    return {
                        "response_action": "errors",
                        "errors": {
                            "persona_selection": "You don't have access to that assistant."
                        },
                    }

                user_slack_persona = fetch_user_slack_persona(
                    db_session=db_session, sender_id=user_id
                )
                if user_slack_persona:
                    add_slack_persona_for_user(
                        db_session=db_session,
                        persona=persona,
                        user_slack_persona=user_slack_persona,
                    )
                    persona_label = persona.display_name or persona.name
                    response_text = f"Persona '{persona_label}' has been set!"
                    client.chat_postMessage(channel=channel_id, text=response_text)
                    return {"response_action": "clear"}

                else:
                    add_user_slack_persona(
                        db_session=db_session, sender_id=user_id, persona=persona
                    )
                    persona_label = persona.display_name or persona.name
                    client.chat_postMessage(
                        channel=channel_id,
                        text=f"'{persona_label}' has been successfully set as the current persona.",
                    )
                    return {"response_action": "clear"}

            except NoResultFound:
                return {
                    "response_action": "errors",
                    "errors": {"persona_selection": "Error in fetching persona"},
                }

    except Exception as e:
        logger.error(f"Error handling modal submission: {e}")
        return {
            "response_action": "errors",
            "errors": {
                "persona_selection": "Sorry, there was an error updating your persona. Please try again."
            },
        }


def handle_summarize_thread_modal(client: WebClient, payload: dict) -> None:
    """Handle the thread summarization modal submission."""
    try:
        parts = payload["view"]["private_metadata"].split(":")
        channel_id, thread_ts, user_id, is_parent_message = parts

        # Generate and send the summary
        handle_thread_summary(
            channel_id=channel_id,
            thread_ts=thread_ts,
            client=client,
            user_id=user_id,
            is_parent_message=is_parent_message == "1",
        )
    except Exception as e:
        logger.exception("Error handling thread summary modal")
        try:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"Error generating thread summary: {str(e)}",
            )
        except Exception as notify_error:
            logger.error(f"Failed to send error notification: {notify_error}")
