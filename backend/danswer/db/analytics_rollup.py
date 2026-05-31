"""Daily rollup of admin analytics metrics.

Why this exists: chat retention deletes `chat_message` / `chat_session`
rows older than RETENTION_DAYS_CHAT (default 30 days). The analytics
endpoints used to read directly from those tables, so any date range
older than the retention window returned zeros — silent data loss for
operators wanting quarterly / yearly trends.

Solution: pre-aggregate the daily metrics into `analytics_daily_rollup`
BEFORE the retention sweep runs, and have the endpoints read from the
rollup. The rollup table is small (one row per day, ~50 bytes) and never
gets retention-cleaned.

Pipeline:
  - 07:30 UTC: `run_analytics_rollup_task` (Celery beat) recomputes from
    `(last_rolled_up_to - ANALYTICS_LATE_FEEDBACK_BUFFER_DAYS)` through
    today. The checkpoint `last_rolled_up_to` is stored as a JSON row in
    `key_value_store` and advanced to today after a successful run.
    Idempotent — safe to re-run; safe across outages (next run catches
    up from where the last one stopped).
  - 08:00 UTC: `run_retention_policies_task` deletes old chat rows.

The "late feedback" buffer (default 2 days) catches feedback that
arrives slightly after the answer — most reactions are immediate, but
a Slack helper marking "resolved" can take a day or two. We re-process
those days each run so updated counts win via INSERT…ON CONFLICT DO
UPDATE.

Safety cap: even with a stale checkpoint (e.g. task offline for weeks),
the task won't try to scan back further than `RETENTION_DAYS_CHAT - 2`
days, since chat data older than retention is gone and re-computing
would zero out historical rows. Days lost to a long outage stay at
their last-known values rather than being clobbered.

For the initial deploy of this module, run
`backend/scripts/backfill_analytics_rollup.py` once before the next
retention sweep — it walks every historical date that still has chat
data, populates the rollup, AND seeds the checkpoint row. After that,
the daily task takes over.
"""
from __future__ import annotations

import datetime
import os
from collections.abc import Sequence

from sqlalchemy import case
from sqlalchemy import cast
from sqlalchemy import Date
from sqlalchemy import func
from sqlalchemy import literal
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from danswer.configs.constants import MessageType
from danswer.db.engine import get_sqlalchemy_engine
from danswer.db.models import AnalyticsDailyRollup
from danswer.db.models import AnalyticsUserDailyStats
from danswer.db.models import AnalyticsUserFirstSeen
from danswer.db.models import ChatMessage
from danswer.db.models import ChatMessageFeedback
from danswer.db.models import ChatSession
from danswer.utils.logger import setup_logger

logger = setup_logger()


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning(f"Invalid {name}={raw!r}; using default {default}")
        return default


# How far back from the checkpoint to re-process on each run. This is the
# late-feedback grace period: most reactions arrive within minutes, but
# a "resolved" mark from a Slack helper can take a day or two. We recompute
# those days each run so the updated counts win via the upsert.
ANALYTICS_LATE_FEEDBACK_BUFFER_DAYS = _env_int("ANALYTICS_LATE_FEEDBACK_BUFFER_DAYS", 2)

# Checkpoint row in `key_value_store`. Stored as JSON like
# `{"last_rolled_up_to": "2026-05-14"}`. The daily task starts from
# `(last_rolled_up_to - buffer)` and advances the checkpoint to today
# after a successful run.
ROLLUP_CHECKPOINT_KEY = "analytics_rollup_state"


# ---------------------------------------------------------------------------
# Compute helpers — read live tables, return the metrics for one UTC day
# ---------------------------------------------------------------------------


def _day_bounds(
    target_date: datetime.date,
) -> tuple[datetime.datetime, datetime.datetime]:
    """[start, end) UTC datetimes for one calendar day."""
    start = datetime.datetime.combine(
        target_date, datetime.time.min, tzinfo=datetime.timezone.utc
    )
    end = start + datetime.timedelta(days=1)
    return start, end


def _compute_query_metrics_for_date(
    db_session: Session, target_date: datetime.date
) -> dict[str, int]:
    """Total assistant replies + feedback breakdown for one UTC day.

    Mirrors `fetch_query_analytics`'s SQL but returns a flat dict for the
    single date (so we can upsert directly).
    """
    start, end = _day_bounds(target_date)
    row = db_session.execute(
        select(
            func.count(ChatMessage.id),
            func.coalesce(
                func.sum(case((ChatMessageFeedback.is_positive, 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(
                    case(
                        (ChatMessageFeedback.is_positive == False, 1),  # noqa: E712
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (ChatMessageFeedback.predefined_feedback == "resolved", 1),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (ChatMessageFeedback.required_followup.is_(True), 1),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        .join(
            ChatMessageFeedback,
            ChatMessageFeedback.chat_message_id == ChatMessage.id,
            isouter=True,
        )
        .where(ChatMessage.time_sent >= start)
        .where(ChatMessage.time_sent < end)
        .where(ChatMessage.message_type == MessageType.ASSISTANT)
    ).one()
    total_queries, likes, dislikes, resolved, needs_help = row
    return {
        "total_queries": int(total_queries or 0),
        "total_likes": int(likes or 0),
        "total_dislikes": int(dislikes or 0),
        "total_resolved": int(resolved or 0),
        "total_needs_help": int(needs_help or 0),
    }


def _compute_active_users_for_date(
    db_session: Session, target_date: datetime.date
) -> int:
    """Distinct user count of users who asked at least one question on
    `target_date`. Joins chat_message → chat_session for user_id.

    `select_from(ChatMessage)` is explicit because the projection only
    references ChatSession.user_id — without it, SA would pick
    ChatSession as the implicit FROM and then complain when we try to
    join it again.
    """
    start, end = _day_bounds(target_date)
    n = db_session.execute(
        select(func.count(func.distinct(ChatSession.user_id)))
        .select_from(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.chat_session_id)
        .where(ChatMessage.time_sent >= start)
        .where(ChatMessage.time_sent < end)
        .where(ChatMessage.message_type == MessageType.ASSISTANT)
        .where(ChatSession.user_id.is_not(None))
    ).scalar()
    return int(n or 0)


def _compute_slackbot_metrics_for_date(
    db_session: Session, target_date: datetime.date
) -> tuple[int, int]:
    """`(slackbot_total, slackbot_auto_resolved)` for one day.

    Total = Slackbot sessions whose first AI reply landed on `target_date`.
    Auto-resolved = total minus sessions whose latest feedback is negative
    or required_followup. Mirrors the existing /admin/danswerbot logic.
    """
    start, end = _day_bounds(target_date)

    subquery_first_ai_response = (
        db_session.query(
            ChatMessage.chat_session_id.label("chat_session_id"),
            func.min(ChatMessage.id).label("chat_message_id"),
        )
        .select_from(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.chat_session_id)
        .where(
            ChatSession.time_created >= start,
            ChatSession.time_created < end,
            ChatSession.danswerbot_flow.is_(True),
        )
        .where(ChatMessage.message_type == MessageType.ASSISTANT)
        .group_by(ChatMessage.chat_session_id)
        .subquery()
    )

    subquery_last_feedback = (
        db_session.query(
            ChatMessageFeedback.chat_message_id.label("chat_message_id"),
            func.max(ChatMessageFeedback.id).label("max_feedback_id"),
        )
        .group_by(ChatMessageFeedback.chat_message_id)
        .subquery()
    )

    # `select_from(ChatSession)` is explicit because the outer SELECT lists
    # only an aggregate on ChatSession.id plus a sum referencing
    # ChatMessageFeedback — SA 2.x can't infer a single left-side FROM
    # from those, especially with ChatSession also referenced inside the
    # `subquery_first_ai_response`. The EE original got away without this
    # because it also selected `cast(ChatSession.time_created, Date)`
    # which pinned the FROM.
    row = (
        db_session.query(
            func.count(ChatSession.id),
            func.coalesce(
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
                ),
                0,
            ),
        )
        .select_from(ChatSession)
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
        .one()
    )
    total, negatives = int(row[0] or 0), int(row[1] or 0)
    return total, max(0, total - negatives)


# ---------------------------------------------------------------------------
# Upsert (compute + write one day's row)
# ---------------------------------------------------------------------------


def upsert_rollup_for_date(
    db_session: Session, target_date: datetime.date
) -> dict[str, int]:
    """Compute one day's metrics from live tables and INSERT … ON CONFLICT
    DO UPDATE into `analytics_daily_rollup`. Returns the metrics dict."""
    query_metrics = _compute_query_metrics_for_date(db_session, target_date)
    active_users = _compute_active_users_for_date(db_session, target_date)
    slackbot_total, slackbot_auto_resolved = _compute_slackbot_metrics_for_date(
        db_session, target_date
    )

    metrics = {
        **query_metrics,
        "active_users": active_users,
        "slackbot_total": slackbot_total,
        "slackbot_auto_resolved": slackbot_auto_resolved,
    }

    stmt = pg_insert(AnalyticsDailyRollup.__table__).values(date=target_date, **metrics)
    update_cols = {col: stmt.excluded[col] for col in metrics.keys()}
    update_cols["rolled_up_at"] = func.now()
    stmt = stmt.on_conflict_do_update(
        index_elements=[AnalyticsDailyRollup.date],
        set_=update_cols,
    )
    db_session.execute(stmt)
    db_session.commit()
    return metrics


def capture_first_seen_for_date(
    db_session: Session, target_date: datetime.date
) -> None:
    """Record ``first_seen_date`` for every user active on ``target_date``
    who isn't already in ``analytics_user_first_seen``.

    INSERT … SELECT … ON CONFLICT (user_id) DO NOTHING: a user already
    present keeps their stored date, so first-seen never moves forward.
    Because :func:`run_rollup` walks dates ascending, the earliest date in
    the processed window on which a user appears is the one recorded — and
    for the full backfill that's their true first-ever day. Once written,
    the row is immune to chat retention deletes (this is the whole point:
    the adoption curve must outlive the raw chat_message rows)."""
    start, end = _day_bounds(target_date)
    active_user_ids = (
        select(
            ChatSession.user_id.label("user_id"),
            literal(target_date, Date).label("first_seen_date"),
        )
        .select_from(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.chat_session_id)
        .where(ChatMessage.time_sent >= start)
        .where(ChatMessage.time_sent < end)
        .where(ChatMessage.message_type == MessageType.ASSISTANT)
        .where(ChatSession.user_id.is_not(None))
        .distinct()
    )
    stmt = (
        pg_insert(AnalyticsUserFirstSeen.__table__)
        .from_select(["user_id", "first_seen_date"], active_user_ids)
        .on_conflict_do_nothing(index_elements=[AnalyticsUserFirstSeen.user_id])
    )
    db_session.execute(stmt)
    db_session.commit()


def upsert_user_daily_stats_for_date(
    db_session: Session, target_date: datetime.date
) -> None:
    """Upsert one row per active user for ``target_date`` into
    ``analytics_user_daily_stats`` (message / like / dislike counts).

    Single INSERT … SELECT … ON CONFLICT (user_id, date) DO UPDATE, so a
    re-run over the sliding window recomputes that day's per-user counts
    (reflecting late feedback). Once written the rows outlive the raw
    chat_message rows that retention deletes — the leaderboard reads this
    aggregate, so it spans full history rather than the last
    RETENTION_DAYS_CHAT."""
    start, end = _day_bounds(target_date)
    per_user = (
        select(
            ChatSession.user_id.label("user_id"),
            literal(target_date, Date).label("date"),
            func.count(ChatMessage.id).label("message_count"),
            func.coalesce(
                func.sum(case((ChatMessageFeedback.is_positive, 1), else_=0)), 0
            ).label("like_count"),
            func.coalesce(
                func.sum(
                    case(
                        (ChatMessageFeedback.is_positive == False, 1),  # noqa: E712
                        else_=0,
                    )
                ),
                0,
            ).label("dislike_count"),
        )
        .select_from(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.chat_session_id)
        .outerjoin(
            ChatMessageFeedback,
            ChatMessageFeedback.chat_message_id == ChatMessage.id,
        )
        .where(ChatMessage.time_sent >= start)
        .where(ChatMessage.time_sent < end)
        .where(ChatMessage.message_type == MessageType.ASSISTANT)
        .where(ChatSession.user_id.is_not(None))
        .group_by(ChatSession.user_id)
    )
    stmt = pg_insert(AnalyticsUserDailyStats.__table__).from_select(
        ["user_id", "date", "message_count", "like_count", "dislike_count"],
        per_user,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            AnalyticsUserDailyStats.user_id,
            AnalyticsUserDailyStats.date,
        ],
        set_={
            "message_count": stmt.excluded.message_count,
            "like_count": stmt.excluded.like_count,
            "dislike_count": stmt.excluded.dislike_count,
            "rolled_up_at": func.now(),
        },
    )
    db_session.execute(stmt)
    db_session.commit()


# ---------------------------------------------------------------------------
# Batch operations — sliding window (daily task) + full backfill
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Checkpoint state in key_value_store
# ---------------------------------------------------------------------------


def _get_checkpoint(db_session: Session) -> datetime.date | None:
    """Read `last_rolled_up_to` from `key_value_store`. Returns None if
    the row is absent or malformed (first run after deploy)."""
    payload = db_session.execute(
        text("SELECT value FROM key_value_store WHERE key = :k"),
        {"k": ROLLUP_CHECKPOINT_KEY},
    ).scalar()
    if not payload or not isinstance(payload, dict):
        return None
    raw = payload.get("last_rolled_up_to")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        logger.warning(
            f"Analytics rollup checkpoint malformed: "
            f"{ROLLUP_CHECKPOINT_KEY}.last_rolled_up_to={raw!r}; ignoring."
        )
        return None


def _set_checkpoint(db_session: Session, last_rolled_up_to: datetime.date) -> None:
    """Upsert the checkpoint row in `key_value_store`. Pure SQL upsert
    via INSERT…ON CONFLICT — avoids a separate read."""
    payload = {"last_rolled_up_to": last_rolled_up_to.isoformat()}
    db_session.execute(
        text(
            """
            INSERT INTO key_value_store (key, value)
            VALUES (:k, CAST(:v AS jsonb))
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value
            """
        ),
        {"k": ROLLUP_CHECKPOINT_KEY, "v": _json_dumps(payload)},
    )
    db_session.commit()


def _json_dumps(obj: dict) -> str:
    # Local import so the analytics_rollup module's import surface
    # stays minimal — `json` is stdlib but we only need it here.
    import json

    return json.dumps(obj)


def _safe_lookback_floor(today: datetime.date) -> datetime.date:
    """Earliest date the rollup task is allowed to recompute, honoring
    chat retention. We refuse to scan beyond `(today - (RETENTION_DAYS_CHAT - 2))`
    because chat data older than that is already deleted; re-processing
    those days would zero out historical totals.

    Imports `RETENTION_DAYS_CHAT` lazily to avoid pulling the retention
    module's side effects at import time on this hot path.
    """
    from danswer.db.retention import RETENTION_DAYS_CHAT

    if RETENTION_DAYS_CHAT <= 0:
        # Chat retention disabled — chat data lives forever, so no floor
        # needed. Cap at "a year ago" just to keep first-run windows sane.
        return today - datetime.timedelta(days=365)
    return today - datetime.timedelta(days=max(2, RETENTION_DAYS_CHAT - 2))


def run_rollup(today: datetime.date | None = None) -> int:
    """Run the daily rollup. Recomputes from
    `(last_rolled_up_to - ANALYTICS_LATE_FEEDBACK_BUFFER_DAYS)` through
    today, then advances the checkpoint to today. Idempotent.

    First-run handling (no checkpoint yet): defaults to `today - buffer`,
    just refreshing the most recent few days. The backfill CLI is the
    intended way to populate historical rows the first time.

    Outage handling: if the task hasn't run for N days, the next run's
    window is `[checkpoint - buffer, today]` and naturally catches up,
    capped by `_safe_lookback_floor` so we never re-process beyond
    retention. Returns the number of dates upserted.
    """
    today = today or datetime.datetime.now(tz=datetime.timezone.utc).date()
    engine = get_sqlalchemy_engine()
    with Session(engine) as db_session:
        checkpoint = _get_checkpoint(db_session)
        if checkpoint is None:
            start = today - datetime.timedelta(days=ANALYTICS_LATE_FEEDBACK_BUFFER_DAYS)
            logger.info(
                "Analytics rollup: no checkpoint found (first run after "
                f"deploy?); processing last {ANALYTICS_LATE_FEEDBACK_BUFFER_DAYS}+1 "
                "days. Run backfill_analytics_rollup.py to populate history."
            )
        else:
            start = checkpoint - datetime.timedelta(
                days=ANALYTICS_LATE_FEEDBACK_BUFFER_DAYS
            )

        floor = _safe_lookback_floor(today)
        if start < floor:
            logger.warning(
                f"Analytics rollup: checkpoint {checkpoint} would push the "
                f"window back to {start}, but retention has likely deleted "
                f"chat data older than {floor}. Capping the window at "
                f"{floor} — older days keep their last-known rollup values."
            )
            start = floor

        n = 0
        current = start
        while current <= today:
            upsert_rollup_for_date(db_session, current)
            # Capture first-seen + per-user daily stats in the same ascending
            # pass, before retention can delete the day's chat rows (rollup
            # runs 07:30, sweep 08:00).
            capture_first_seen_for_date(db_session, current)
            upsert_user_daily_stats_for_date(db_session, current)
            current += datetime.timedelta(days=1)
            n += 1

        _set_checkpoint(db_session, today)
        logger.info(
            f"Analytics rollup: processed {n} day(s) [{start} → {today}], "
            f"checkpoint advanced to {today}"
        )
    return n


def backfill_all_rollups(start_date: datetime.date, end_date: datetime.date) -> int:
    """Walk every date in [start_date, end_date] and upsert. Used by the
    one-time backfill CLI before retention starts deleting chat data.

    Sets the checkpoint to `end_date` on completion so the next daily
    run's window starts from `(end_date - buffer)` instead of the
    pre-deploy default.
    """
    if start_date > end_date:
        raise ValueError(
            f"backfill_all_rollups: start_date {start_date} > end_date {end_date}"
        )
    engine = get_sqlalchemy_engine()
    n = 0
    with Session(engine) as db_session:
        current = start_date
        while current <= end_date:
            upsert_rollup_for_date(db_session, current)
            # Walk ascending so each user's first_seen_date is their true
            # first-ever active day across all currently-available history.
            capture_first_seen_for_date(db_session, current)
            upsert_user_daily_stats_for_date(db_session, current)
            current += datetime.timedelta(days=1)
            n += 1
            if n % 30 == 0:
                logger.info(
                    f"backfill_all_rollups: processed {n} days "
                    f"(through {current - datetime.timedelta(days=1)})"
                )
        _set_checkpoint(db_session, end_date)
    return n


# ---------------------------------------------------------------------------
# Read-from-rollup helpers — what the API endpoints call
# ---------------------------------------------------------------------------


def fetch_query_analytics_from_rollup(
    start: datetime.datetime,
    end: datetime.datetime,
    db_session: Session,
) -> Sequence[tuple[int, int, int, int, int, datetime.date]]:
    """Same shape as `db.analytics.fetch_query_analytics` but reads from
    the rollup table — survives chat retention deletes.

    Returns rows of `(total_queries, total_likes, total_dislikes,
    total_resolved, total_needs_help, date)` for each day in
    [start.date(), end.date()] that has a rollup row. Days with no rollup
    yet (e.g. today, before 07:30 UTC) are absent — caller should treat
    missing dates as zero / not-yet-rolled-up.
    """
    stmt = (
        select(
            AnalyticsDailyRollup.total_queries,
            AnalyticsDailyRollup.total_likes,
            AnalyticsDailyRollup.total_dislikes,
            AnalyticsDailyRollup.total_resolved,
            AnalyticsDailyRollup.total_needs_help,
            AnalyticsDailyRollup.date,
        )
        .where(AnalyticsDailyRollup.date >= cast(start, Date))
        .where(AnalyticsDailyRollup.date <= cast(end, Date))
        .order_by(AnalyticsDailyRollup.date)
    )
    return db_session.execute(stmt).all()  # type: ignore


def fetch_user_analytics_from_rollup(
    start: datetime.datetime,
    end: datetime.datetime,
    db_session: Session,
) -> Sequence[tuple[int, datetime.date]]:
    """Distinct-user count per day from the rollup. Returns
    `(active_users, date)` tuples ordered by date."""
    stmt = (
        select(AnalyticsDailyRollup.active_users, AnalyticsDailyRollup.date)
        .where(AnalyticsDailyRollup.date >= cast(start, Date))
        .where(AnalyticsDailyRollup.date <= cast(end, Date))
        .order_by(AnalyticsDailyRollup.date)
    )
    return db_session.execute(stmt).all()  # type: ignore


def fetch_danswerbot_analytics_from_rollup(
    start: datetime.datetime,
    end: datetime.datetime,
    db_session: Session,
) -> Sequence[tuple[int, int, datetime.date]]:
    """Slackbot daily stats from the rollup. Returns
    `(slackbot_total, slackbot_auto_resolved, date)` tuples."""
    stmt = (
        select(
            AnalyticsDailyRollup.slackbot_total,
            AnalyticsDailyRollup.slackbot_auto_resolved,
            AnalyticsDailyRollup.date,
        )
        .where(AnalyticsDailyRollup.date >= cast(start, Date))
        .where(AnalyticsDailyRollup.date <= cast(end, Date))
        .order_by(AnalyticsDailyRollup.date)
    )
    return db_session.execute(stmt).all()  # type: ignore


def get_earliest_chat_date(db_session: Session) -> datetime.date | None:
    """Earliest `chat_session.time_created::date`, or None if the table
    is empty. Used by the backfill CLI as the default --start."""
    earliest = db_session.execute(
        select(func.min(cast(ChatSession.time_created, Date)))
    ).scalar()
    return earliest if isinstance(earliest, datetime.date) else None
