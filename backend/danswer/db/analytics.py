"""Analytics aggregate queries for the admin /analytics page.

Lives in the community (open-source) module, parallel to (not depending
on) `ee/danswer/db/analytics.py`. The shape mirrors upstream Onyx EE so
the same frontend code can drive either backend, but the implementation
imports only from `danswer.*` — never from `ee.*`.

Three time-series aggregates, all daily-bucketed:
  - fetch_query_analytics: total assistant messages + likes + dislikes per day
  - fetch_per_user_query_analytics: same, broken out per user (used to
    derive distinct-user counts)
  - fetch_danswerbot_analytics: Slackbot session count + sessions
    flagged as "needs help" (negative or required-followup feedback) per day
"""
import datetime
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import case
from sqlalchemy import cast
from sqlalchemy import Date
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.orm import Session

from danswer.configs.constants import MessageType
from danswer.db.models import ChatMessage
from danswer.db.models import ChatMessageFeedback
from danswer.db.models import ChatSession
from danswer.db.models import Connector
from danswer.db.models import ConnectorCredentialPair
from danswer.db.models import Document


def fetch_query_analytics(
    start: datetime.datetime,
    end: datetime.datetime,
    db_session: Session,
) -> Sequence[tuple[int, int, int, int, int, datetime.date]]:
    """Daily aggregates of assistant chat messages and their feedback.

    Returns rows of:
        (total_queries, total_likes, total_dislikes,
         total_resolved, total_needs_help, date)

    where each "query" is one assistant-message reply, and each feedback
    bucket counts FEEDBACK ROWS (not messages) — a single message with
    two feedback events shows up twice. The frontend's "strict NPS"
    treats likes + resolved as promoters and dislikes + needs-help as
    detractors:

        NPS_strict = (P - D) / (P + D) * 100
                   = ((likes + resolved) - (dislikes + needs_help)) /
                     ((likes + resolved) + (dislikes + needs_help)) * 100

    `total_resolved` comes from `predefined_feedback = 'resolved'` rows —
    written by the Slackbot's "I'm all set!" / "Mark Resolved" buttons
    (see danswerbot/slack/handlers/handle_buttons.py). Older bot replies
    don't have these rows yet; the count grows as users click resolved
    going forward.

    `total_needs_help` counts `required_followup IS TRUE` rows — the "I
    need more help" button in the Slackbot.
    """
    stmt = (
        select(
            func.count(ChatMessage.id),
            func.sum(case((ChatMessageFeedback.is_positive, 1), else_=0)),
            func.sum(
                case(
                    (ChatMessageFeedback.is_positive == False, 1),  # noqa: E712
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (ChatMessageFeedback.predefined_feedback == "resolved", 1),
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (ChatMessageFeedback.required_followup.is_(True), 1),
                    else_=0,
                )
            ),
            cast(ChatMessage.time_sent, Date),
        )
        .join(
            ChatMessageFeedback,
            ChatMessageFeedback.chat_message_id == ChatMessage.id,
            isouter=True,
        )
        .where(ChatMessage.time_sent >= start)
        .where(ChatMessage.time_sent <= end)
        .where(ChatMessage.message_type == MessageType.ASSISTANT)
        .group_by(cast(ChatMessage.time_sent, Date))
        .order_by(cast(ChatMessage.time_sent, Date))
    )

    return db_session.execute(stmt).all()  # type: ignore


def fetch_per_user_query_analytics(
    start: datetime.datetime,
    end: datetime.datetime,
    db_session: Session,
) -> Sequence[tuple[int, int, int, datetime.date, UUID]]:
    """Same as `fetch_query_analytics` but grouped by user_id too.

    The /analytics/admin/user endpoint folds this down to a count of
    distinct users per day.
    """
    stmt = (
        select(
            func.count(ChatMessage.id),
            func.sum(case((ChatMessageFeedback.is_positive, 1), else_=0)),
            func.sum(
                case(
                    (ChatMessageFeedback.is_positive == False, 1),  # noqa: E712
                    else_=0,
                )
            ),
            cast(ChatMessage.time_sent, Date),
            ChatSession.user_id,
        )
        .join(ChatSession, ChatSession.id == ChatMessage.chat_session_id)
        .where(ChatMessage.time_sent >= start)
        .where(ChatMessage.time_sent <= end)
        .where(ChatMessage.message_type == MessageType.ASSISTANT)
        .group_by(cast(ChatMessage.time_sent, Date), ChatSession.user_id)
        .order_by(cast(ChatMessage.time_sent, Date), ChatSession.user_id)
    )

    return db_session.execute(stmt).all()  # type: ignore


def fetch_danswerbot_analytics(
    start: datetime.datetime,
    end: datetime.datetime,
    db_session: Session,
) -> Sequence[tuple[int, int, datetime.date]]:
    """Daily Slackbot stats: total sessions and sessions flagged "needs help".

    Returns rows of (total_sessions, total_negatives, date). The endpoint
    layer derives `auto_resolved = max(0, total - total_negatives)`.

    "needs help" = the FIRST AI reply in the session has its LATEST
    feedback marked as either negative OR required_followup. Sessions
    with no feedback are NOT counted as negative (we want to give the
    bot the benefit of the doubt when nobody clicked thumbs-down).
    """
    # First AI message per Danswerbot session in the window.
    subquery_first_ai_response = (
        db_session.query(
            ChatMessage.chat_session_id.label("chat_session_id"),
            func.min(ChatMessage.id).label("chat_message_id"),
        )
        .join(ChatSession, ChatSession.id == ChatMessage.chat_session_id)
        .where(
            ChatSession.time_created >= start,
            ChatSession.time_created <= end,
            ChatSession.danswerbot_flow.is_(True),
        )
        .where(ChatMessage.message_type == MessageType.ASSISTANT)
        .group_by(ChatMessage.chat_session_id)
        .subquery()
    )

    # Most recent feedback row per chat_message that has any feedback.
    subquery_last_feedback = (
        db_session.query(
            ChatMessageFeedback.chat_message_id.label("chat_message_id"),
            func.max(ChatMessageFeedback.id).label("max_feedback_id"),
        )
        .group_by(ChatMessageFeedback.chat_message_id)
        .subquery()
    )

    results = (
        db_session.query(
            func.count(ChatSession.id).label("total_sessions"),
            func.sum(
                case(
                    (
                        or_(
                            ChatMessageFeedback.is_positive.is_(False),
                            ChatMessageFeedback.required_followup,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("negative_answer"),
            cast(ChatSession.time_created, Date).label("session_date"),
        )
        .join(
            subquery_first_ai_response,
            ChatSession.id == subquery_first_ai_response.c.chat_session_id,
        )
        .outerjoin(
            subquery_last_feedback,
            subquery_first_ai_response.c.chat_message_id
            == subquery_last_feedback.c.chat_message_id,
        )
        .outerjoin(
            ChatMessageFeedback,
            ChatMessageFeedback.id == subquery_last_feedback.c.max_feedback_id,
        )
        .group_by(cast(ChatSession.time_created, Date))
        .order_by(cast(ChatSession.time_created, Date))
        .all()
    )

    return results


# ---------------------------------------------------------------------------
# Snapshot helpers (point-in-time, no date range)
# ---------------------------------------------------------------------------


def fetch_total_docs_indexed(db_session: Session) -> tuple[int, int]:
    """Return `(total_docs_indexed, unique_docs)`.

    `total_docs_indexed` is the sum of the denormalized
    `connector_credential_pair.total_docs_indexed` counter — counts the
    same physical doc once per cc-pair that indexed it. This matches what
    the indexing-status page shows per-row, so the KPI total = sum of
    visible per-cc-pair counts.

    `unique_docs` is `count(*) FROM document`, the number of distinct
    physical documents in Postgres. Lower than `total_docs_indexed`
    when the same document is indexed by multiple cc-pairs.
    """
    total_docs_indexed = (
        db_session.execute(
            select(func.coalesce(func.sum(ConnectorCredentialPair.total_docs_indexed), 0))
        ).scalar()
        or 0
    )
    unique_docs = (
        db_session.execute(select(func.count(Document.id))).scalar() or 0
    )
    return int(total_docs_indexed), int(unique_docs)


def fetch_docs_per_source(db_session: Session) -> Sequence[tuple[str, int]]:
    """Return rows of `(source, total_docs_indexed)` ordered DESC.

    Sources with zero docs (newly-added connectors that haven't run yet)
    are included so operators can see them in the chart and notice they
    haven't started indexing. Excluding them is harder to debug.
    """
    stmt = (
        select(
            Connector.source,
            func.coalesce(func.sum(ConnectorCredentialPair.total_docs_indexed), 0),
        )
        .join(
            ConnectorCredentialPair,
            ConnectorCredentialPair.connector_id == Connector.id,
            isouter=True,
        )
        .group_by(Connector.source)
        .order_by(
            func.coalesce(func.sum(ConnectorCredentialPair.total_docs_indexed), 0).desc()
        )
    )
    return [(str(src), int(n)) for src, n in db_session.execute(stmt).all()]


def fetch_slack_bot_channel_stats(db_session: Session) -> tuple[int, int]:
    """Return `(total_configs, distinct_channels_enabled)`.

    `slack_bot_config.channel_config` is a JSONB blob with a
    `channel_names: list[str]` key. Each row's config can target many
    channels; we unroll all rows' arrays and count distinct channel
    names across the whole table. A channel listed in two configs counts
    once.

    Uses `jsonb_array_elements_text` lateral expansion in raw SQL — the
    SQLAlchemy DSL for jsonb-array unrolling is verbose enough that raw
    SQL is the cleaner option here.
    """
    total_configs = (
        db_session.execute(
            text("SELECT count(*) FROM slack_bot_config")
        ).scalar()
        or 0
    )
    distinct_channels = (
        db_session.execute(
            text(
                """
                SELECT count(DISTINCT chan)
                FROM slack_bot_config,
                     jsonb_array_elements_text(channel_config -> 'channel_names')
                       AS chan
                """
            )
        ).scalar()
        or 0
    )
    return int(total_configs), int(distinct_channels)
