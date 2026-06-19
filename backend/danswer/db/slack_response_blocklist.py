"""Helpers for the Slack response blocklist — senders (by email) whose messages
should NOT trigger a Darwin response. DB-driven (see
db/models.py::SlackBotResponseBlocklist); consumed by
danswerbot/slack/handlers/handle_message.py.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from danswer.db.models import SlackBotResponseBlocklist


def get_slack_response_blocklisted_emails(db_session: Session) -> set[str]:
    """Lowercased set of emails whose Slack messages must not trigger a response.

    Returns an empty set when nothing is blocklisted, which lets the caller skip
    the per-message Slack `users_info` lookup entirely.
    """
    emails = db_session.scalars(select(SlackBotResponseBlocklist.email)).all()
    return {email.lower() for email in emails}


def add_email_to_slack_response_blocklist(
    email: str, db_session: Session
) -> SlackBotResponseBlocklist:
    entry = SlackBotResponseBlocklist(email=email.strip().lower())
    db_session.add(entry)
    db_session.commit()
    return entry


def remove_email_from_slack_response_blocklist(
    email: str, db_session: Session
) -> None:
    db_session.query(SlackBotResponseBlocklist).filter(
        SlackBotResponseBlocklist.email == email.strip().lower()
    ).delete()
    db_session.commit()
