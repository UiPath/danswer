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
from danswer.db.models import AnalyticsPersonaDailyStats
from danswer.db.models import AnalyticsUserDailyStats
from danswer.db.models import AnalyticsUserFirstSeen
from danswer.db.models import ChatMessage
from danswer.db.models import ChatMessageFeedback
from danswer.db.models import ChatSession
from danswer.db.models import Connector
from danswer.db.models import ConnectorCredentialPair
from danswer.db.models import Document
from danswer.db.models import DocumentSet
from danswer.db.models import Persona
from danswer.db.models import Persona__DocumentSet
from danswer.db.models import User


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


def fetch_user_adoption(
    start: datetime.datetime,
    end: datetime.datetime,
    db_session: Session,
) -> list[tuple[datetime.date, int, int]]:
    """Per-day ``(date, new_users, cumulative_users)`` from the durable
    ``analytics_user_first_seen`` table — the chat adoption curve.

    Read from the aggregate, NOT raw chat, so it spans the full history
    regardless of RETENTION_DAYS_CHAT. ``cumulative_users`` folds in users
    whose first-seen predates ``start`` so the running total is continuous.
    Only days on which at least one user first appeared are returned.
    """
    start_date = start.date()
    end_date = end.date()
    rows = db_session.execute(
        select(
            AnalyticsUserFirstSeen.first_seen_date,
            func.count().label("new_users"),
        )
        .where(AnalyticsUserFirstSeen.first_seen_date <= end_date)
        .group_by(AnalyticsUserFirstSeen.first_seen_date)
        .order_by(AnalyticsUserFirstSeen.first_seen_date)
    ).all()

    out: list[tuple[datetime.date, int, int]] = []
    cumulative = 0
    for day, new_users in rows:
        cumulative += int(new_users)
        if day >= start_date:
            out.append((day, int(new_users), cumulative))
    return out


def fetch_per_user_chat_stats(
    start: datetime.datetime,
    end: datetime.datetime,
    db_session: Session,
    limit: int = 100,
) -> Sequence[tuple[UUID, str, int, int, int, datetime.date]]:
    """Top ``limit`` users by message volume over ``[start, end]``, with
    like/dislike tallies and last-active date, joined to ``user`` for email.

    Reads the durable ``analytics_user_daily_stats`` aggregate (upserted
    daily by the rollup), NOT raw chat — so it spans the full history
    regardless of RETENTION_DAYS_CHAT. Inner join on ``user`` drops
    anonymous sessions and deleted users (whose counts persist in the
    aggregate but shouldn't surface by email).
    """
    start_date = start.date()
    end_date = end.date()
    stmt = (
        # SA's select() overloads don't type a 6-col sum/max projection;
        # the runtime is fine (the existing analytics selects do the same).
        select(  # type: ignore[call-overload]
            User.id,
            User.email,
            func.coalesce(func.sum(AnalyticsUserDailyStats.message_count), 0),
            func.coalesce(func.sum(AnalyticsUserDailyStats.like_count), 0),
            func.coalesce(func.sum(AnalyticsUserDailyStats.dislike_count), 0),
            func.max(AnalyticsUserDailyStats.date),
        )
        .select_from(AnalyticsUserDailyStats)
        .join(User, User.id == AnalyticsUserDailyStats.user_id)
        .where(AnalyticsUserDailyStats.date >= start_date)
        .where(AnalyticsUserDailyStats.date <= end_date)
        .group_by(User.id, User.email)
        .order_by(func.sum(AnalyticsUserDailyStats.message_count).desc())
        .limit(limit)
    )
    return db_session.execute(stmt).all()  # type: ignore


def fetch_persona_usage(
    start: datetime.datetime,
    end: datetime.datetime,
    db_session: Session,
    limit: int = 100,
) -> Sequence[tuple[int, str, int, int, int, int, datetime.date]]:
    """Top ``limit`` assistants by message volume over ``[start, end]`` from
    the durable ``analytics_persona_daily_stats`` aggregate — spans full
    history. Joined to ``persona`` for the name (a deleted assistant drops
    off). Returns (persona_id, name, sessions, messages, likes, dislikes,
    last_active)."""
    start_date = start.date()
    end_date = end.date()
    stmt = (
        select(  # type: ignore[call-overload]
            Persona.id,
            Persona.name,
            func.coalesce(func.sum(AnalyticsPersonaDailyStats.session_count), 0),
            func.coalesce(func.sum(AnalyticsPersonaDailyStats.message_count), 0),
            func.coalesce(func.sum(AnalyticsPersonaDailyStats.like_count), 0),
            func.coalesce(func.sum(AnalyticsPersonaDailyStats.dislike_count), 0),
            func.max(AnalyticsPersonaDailyStats.date),
        )
        .select_from(AnalyticsPersonaDailyStats)
        .join(Persona, Persona.id == AnalyticsPersonaDailyStats.persona_id)
        .where(AnalyticsPersonaDailyStats.date >= start_date)
        .where(AnalyticsPersonaDailyStats.date <= end_date)
        .group_by(Persona.id, Persona.name)
        .order_by(func.sum(AnalyticsPersonaDailyStats.message_count).desc())
        .limit(limit)
    )
    return db_session.execute(stmt).all()  # type: ignore


def fetch_document_set_usage(
    start: datetime.datetime,
    end: datetime.datetime,
    db_session: Session,
    limit: int = 100,
) -> Sequence[tuple[int, str, int]]:
    """APPROXIMATE "datasets in use" over ``[start, end]``: each assistant's
    message volume attributed to every document set currently attached to it
    (via persona__document_set).

    This is availability-weighted, not retrieval-truth: an assistant's
    messages are counted toward ALL its document sets (so totals can exceed
    the real query count), and it uses CURRENT attachments (membership drift
    isn't historical). There is no per-query record of which document set
    actually served a result, so this is the best durable signal without new
    instrumentation. Returns (document_set_id, name, attributed_messages).
    """
    start_date = start.date()
    end_date = end.date()
    stmt = (
        select(
            DocumentSet.id,
            DocumentSet.name,
            func.coalesce(func.sum(AnalyticsPersonaDailyStats.message_count), 0),
        )
        .select_from(AnalyticsPersonaDailyStats)
        .join(
            Persona__DocumentSet,
            Persona__DocumentSet.persona_id == AnalyticsPersonaDailyStats.persona_id,
        )
        .join(DocumentSet, DocumentSet.id == Persona__DocumentSet.document_set_id)
        .where(AnalyticsPersonaDailyStats.date >= start_date)
        .where(AnalyticsPersonaDailyStats.date <= end_date)
        .group_by(DocumentSet.id, DocumentSet.name)
        .order_by(func.sum(AnalyticsPersonaDailyStats.message_count).desc())
        .limit(limit)
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
            select(
                func.coalesce(func.sum(ConnectorCredentialPair.total_docs_indexed), 0)
            )
        ).scalar()
        or 0
    )
    unique_docs = db_session.execute(select(func.count(Document.id))).scalar() or 0
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
            func.coalesce(
                func.sum(ConnectorCredentialPair.total_docs_indexed), 0
            ).desc()
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
        db_session.execute(text("SELECT count(*) FROM slack_bot_config")).scalar() or 0
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
