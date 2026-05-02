"""DB retention / cleanup policies.

Periodic deletion of stale rows from tables that grow without bound. Each
policy declares a retention window (in days) configurable via env var
plus the SQL or function that performs the delete. The executor runs
every policy under a single Postgres advisory lock so two runs can't
overlap, and deletes in batches so a multi-million-row first run can't
hold table locks for minutes.

This is the lean equivalent of the per-table `check_for_*_cleanup` Celery
tasks in upstream Onyx, adapted for this fork's Postgres broker (which
materializes Kombu's queue as SQL tables — Onyx uses Redis and doesn't
have to worry about kombu_message bloat).

Policies registered here:
  - kombu_message       : Celery's broker storage when SQLAlchemy is the broker.
  - task_queue_jobs     : This fork's Celery task tracking (TaskQueueState).
  - index_attempt       : Indexing run history (DISABLED by default; opt in
                          by setting RETENTION_DAYS_INDEX_ATTEMPT to a positive
                          integer).
  - permission_sync_run : Permission sync run history (terminal rows only —
                          `in_progress` rows are kept regardless of age).
  - usage_reports       : UsageReport rows + their associated file_store rows
                          and LO blobs (FK-safe deletion).
  - chat                : chat_session + chat_message + chat-attached
                          file_store rows + LO blobs + orphaned search_doc
                          rows (mirrors db/chat.py UI-driven delete path).

Run from the daily beat task `run_retention_policies_task` or one-shot
via `backend/scripts/cleanup_stale_db.py`.

NOTE on disk reclamation: Postgres `DELETE` only marks rows dead — disk
space is reclaimed by autovacuum (or manual `VACUUM`) at some later
point. For typical daily sweeps this is a non-issue; autovacuum keeps up.
For the FIRST run against accumulated bloat (e.g. millions of orphaned
kombu_message rows), expect:
  1. Disk usage stays the same immediately after the DELETE.
  2. autovacuum will reclaim space within minutes-to-hours.
  3. If you need to reclaim immediately (e.g. low disk pressure):
       VACUUM (VERBOSE, ANALYZE) kombu_message;
     `VACUUM FULL` is also possible but takes an AccessExclusiveLock and
     blocks all reads/writes — avoid unless absolutely necessary.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from danswer.db.engine import get_sqlalchemy_engine
from danswer.db.pg_file_store import delete_lobj_by_name
from danswer.utils.logger import setup_logger

logger = setup_logger()


# ---------------------------------------------------------------------------
# Defaults — overridable by env var
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return max(minimum, v)
    except ValueError:
        logger.warning(f"Invalid {name}={raw!r}; using default {default}")
        return default


RETENTION_DAYS_KOMBU = _env_int("RETENTION_DAYS_KOMBU", 7)
RETENTION_DAYS_TASK_QUEUE = _env_int("RETENTION_DAYS_TASK_QUEUE", 30)
# index_attempt rows are retained indefinitely by default — they're the
# debug history for every indexing run, including failures, and operators
# tend to want them around for postmortems on slow connectors. The
# `index_attempt` policy stays in the registry so you can opt into time-
# based + keep-last-N pruning by setting RETENTION_DAYS_INDEX_ATTEMPT to a
# positive integer; the executor short-circuits when days <= 0.
RETENTION_DAYS_INDEX_ATTEMPT = _env_int("RETENTION_DAYS_INDEX_ATTEMPT", 0)
RETENTION_DAYS_CHAT = _env_int("RETENTION_DAYS_CHAT", 30)
RETENTION_DAYS_USAGE_REPORTS = _env_int("RETENTION_DAYS_USAGE_REPORTS", 90)
RETENTION_DAYS_PERMISSION_SYNC = _env_int("RETENTION_DAYS_PERMISSION_SYNC", 30)
RETENTION_KEEP_LAST_N_INDEX_ATTEMPTS = _env_int(
    "RETENTION_KEEP_LAST_N_INDEX_ATTEMPTS", 20
)

# Batch size for incremental DELETEs. Each batch is a separate transaction
# so locks release between iterations and concurrent writers (Celery
# enqueue, indexer status updates) aren't starved. 5000 is a reasonable
# default — large enough to be efficient, small enough that any single
# DELETE finishes in well under a second on a modern Postgres.
RETENTION_BATCH_SIZE = _env_int("RETENTION_BATCH_SIZE", 5000, minimum=100)

# Hard ceiling on number of batches per policy per run. Stops a runaway
# DELETE from spinning forever if `criterion < cutoff` somehow keeps
# matching new rows (e.g. clock skew, very large windows). At default
# settings, 200 * 5000 = 1M rows per policy per run; the next daily run
# picks up where this one stopped.
RETENTION_MAX_BATCHES = _env_int("RETENTION_MAX_BATCHES", 200, minimum=1)

# Postgres advisory lock id, arbitrary 64-bit int. Keeps two concurrent
# retention runs from racing. Picked from /dev/urandom; no domain meaning.
_RETENTION_ADVISORY_LOCK_ID = 0x52455445_4e54494f  # b"RETENTIO" packed


# ---------------------------------------------------------------------------
# Policy framework
# ---------------------------------------------------------------------------

# A policy function takes (db_session, cutoff, batch_size, dry_run) and
# returns the number of rows it deleted (or *would* delete in dry-run).
# Function-based policies are responsible for their own batching + commit
# loop. SQL-based policies use the framework's batching helper.
PolicyFn = Callable[[Session, datetime, int, bool], int]


@dataclass
class RetentionPolicy:
    name: str
    days: int

    # SQL-based policy: provide both a `count_sql` (used in dry-run, no
    # locks) and a `delete_sql` that selects up to `:batch_size` rows by
    # primary key and deletes them. Both should reference `:cutoff`. The
    # framework loops `delete_sql` until rowcount == 0 (or
    # RETENTION_MAX_BATCHES, whichever comes first).
    count_sql: str | None = None
    delete_sql: str | None = None

    # Function-based policy: for multi-table deletes that need explicit
    # FK ordering. The function handles its own batching + commits.
    fn: PolicyFn | None = None

    # Optional table to ANALYZE after a substantial purge (>= one full
    # batch). Postgres planner stats can drift after deleting millions of
    # rows; ANALYZE refreshes them so subsequent queries pick good plans.
    # Only meaningful for SQL-based policies — function policies that
    # touch multiple tables can do their own ANALYZE if needed.
    analyze_table: str | None = None

    enabled: bool = True

    def __post_init__(self) -> None:
        sql_set = self.count_sql is not None and self.delete_sql is not None
        fn_set = self.fn is not None
        if sql_set == fn_set:
            raise ValueError(
                f"RetentionPolicy {self.name!r} must set EITHER both "
                "(count_sql + delete_sql) OR fn — not both / neither."
            )


# ---------------------------------------------------------------------------
# Per-table policies
# ---------------------------------------------------------------------------

# Kombu's queue storage. The `visible=true` filter is the safety net —
# we never delete a message a worker is currently holding (those have
# `visible=false` until the worker acks or the visibility timeout
# expires). Old + visible = orphaned message that no consumer will ever
# pick up; safe to drop. Batched on `id` (kombu_message PK).
#
# `visible = true` is repeated in the OUTER DELETE WHERE — not just the
# inner SELECT — so a worker leasing a row between the snapshot and the
# DELETE (flipping visible to false) is protected. Without the outer
# filter, the DELETE would match by id alone and could nuke an in-flight
# message. Belt and suspenders.
#
# `kombu_message.timestamp` is tz-naive UTC by Kombu's transport schema;
# the tz-aware `:cutoff` we bind has its tz stripped at psycopg2 bind
# time. Both sides represent UTC, so the comparison is correct in
# practice — but if the Kombu schema ever changes, revisit this.
_KOMBU_COUNT_SQL = """
    SELECT count(*) FROM kombu_message
    WHERE timestamp < :cutoff AND visible = true
"""
_KOMBU_DELETE_SQL = """
    DELETE FROM kombu_message
    WHERE id IN (
        SELECT id FROM kombu_message
        WHERE timestamp < :cutoff AND visible = true
        ORDER BY id
        LIMIT :batch_size
    )
    AND visible = true
"""

# Our own task tracking table. Only delete rows in terminal status —
# never drop a PENDING / STARTED row, those may be alive even if old
# (e.g. a stuck deletion task from a dead worker that we still need to
# notice).
_TASK_QUEUE_COUNT_SQL = """
    SELECT count(*) FROM task_queue_jobs
    WHERE register_time < :cutoff
      AND status IN ('SUCCESS', 'FAILURE')
"""
_TASK_QUEUE_DELETE_SQL = """
    DELETE FROM task_queue_jobs
    WHERE id IN (
        SELECT id FROM task_queue_jobs
        WHERE register_time < :cutoff
          AND status IN ('SUCCESS', 'FAILURE')
        ORDER BY id
        LIMIT :batch_size
    )
"""

# Index attempt history. Keep the last N per (connector, credential,
# embedding_model) so paused-but-not-deleted connectors don't lose all
# their debug history. Only TERMINAL rows are eligible. Time bound is
# secondary — if there are still 20 newer attempts, even a 90-day-old
# row is kept. Batched by limiting the outer DELETE.
#
# Status filter uses UPPERCASE values: the column type is
# `Enum(IndexingStatus, native_enum=False)` which by default stores the
# enum NAME (uppercase 'SUCCESS' / 'FAILED'), not its `.value` (lowercase).
# Verified via `SELECT status FROM index_attempt LIMIT 1`. To store the
# values instead you'd need `values_callable=lambda x: [e.value for e in x]`
# in the column declaration — but the schema doesn't, so uppercase wins.
# There is no CANCELED status in this fork's enum (only in upstream Onyx).
# Don't add it back without checking `db/enums.py`.
_INDEX_ATTEMPT_COUNT_SQL = """
    SELECT count(*) FROM (
        SELECT
            id,
            row_number() OVER (
                PARTITION BY connector_id, credential_id, embedding_model_id
                ORDER BY time_created DESC
            ) AS rn
        FROM index_attempt
        WHERE status IN ('SUCCESS', 'FAILED')
          AND time_created < :cutoff
    ) ranked
    WHERE rn > :keep_n
"""
_INDEX_ATTEMPT_DELETE_SQL = """
    DELETE FROM index_attempt
    WHERE id IN (
        SELECT id FROM (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY connector_id, credential_id, embedding_model_id
                    ORDER BY time_created DESC
                ) AS rn
            FROM index_attempt
            WHERE status IN ('SUCCESS', 'FAILED')
              AND time_created < :cutoff
        ) ranked
        WHERE rn > :keep_n
        LIMIT :batch_size
    )
"""


# Permission sync run history. Only delete TERMINAL rows (`success`,
# `failed`); never drop `in_progress` rows even if old, since those may
# represent stuck syncs an operator still needs to notice.
_PERMISSION_SYNC_COUNT_SQL = """
    SELECT count(*) FROM permission_sync_run
    WHERE updated_at < :cutoff
      AND status IN ('success', 'failed')
"""
_PERMISSION_SYNC_DELETE_SQL = """
    DELETE FROM permission_sync_run
    WHERE id IN (
        SELECT id FROM permission_sync_run
        WHERE updated_at < :cutoff
          AND status IN ('success', 'failed')
        ORDER BY id
        LIMIT :batch_size
    )
"""


def _delete_old_usage_reports(
    db_session: Session,
    cutoff: datetime,
    batch_size: int,
    dry_run: bool,
) -> int:
    """Delete UsageReport rows older than `cutoff` along with their
    associated `file_store` row + LO blob.

    `usage_reports.report_name` is a FK to `file_store.file_name`, so we
    delete the UsageReport row first (releasing the FK) and only then
    drop the file_store row + unlink the LO. Otherwise the file_store
    delete would fail with FK violation while the report still references
    it.

    `delete_lobj_by_name` commits per-file; we keep batches small so the
    overall sweep makes incremental progress even if interrupted.
    """
    rows = db_session.execute(
        text(
            "SELECT id, report_name FROM usage_reports "
            "WHERE time_created < :cutoff ORDER BY id"
        ),
        {"cutoff": cutoff},
    ).all()
    if not rows:
        return 0

    if dry_run:
        logger.info(
            f"usage_reports dry-run: {len(rows)} reports would be deleted "
            "(plus their file_store rows and LO blobs)"
        )
        return len(rows)

    total = 0
    for batch_start in range(0, len(rows), batch_size):
        batch = rows[batch_start : batch_start + batch_size]
        batch_ids = [r[0] for r in batch]
        batch_names = [r[1] for r in batch]

        # 1. Drop the usage_reports rows so the FK on report_name is
        #    released. Commit immediately so the LO+file_store deletes
        #    below run in their own short transactions.
        result = db_session.execute(
            text("DELETE FROM usage_reports WHERE id = ANY(:ids)"),
            {"ids": batch_ids},
        )
        total += int(result.rowcount or 0)
        db_session.commit()

        # 2. Drop each LO blob + file_store row. Per-file because
        #    `delete_lobj_by_name` does its own lookup + commit; if a row
        #    is already gone (concurrent admin delete, etc.) it logs and
        #    moves on rather than blowing up the sweep.
        for name in batch_names:
            try:
                delete_lobj_by_name(name, db_session)
            except Exception as e:
                logger.warning(
                    f"usage_reports: failed to delete file_store/{name}: "
                    f"{e}; usage_reports row already gone, file_store row "
                    "may now be orphaned. Continuing."
                )

    return total


def _delete_old_chat(
    db_session: Session,
    cutoff: datetime,
    batch_size: int,
    dry_run: bool,
) -> int:
    """Delete chat sessions whose newest message is older than `cutoff`,
    along with their messages and the FK-dependent rows.

    Order matters because two FKs on `chat_message` have no ON DELETE
    CASCADE / SET NULL: `chat_message__search_doc.chat_message_id` and
    `tool_call.message_id`. We delete those first, then messages, then
    sessions. `document_retrieval_feedback` and `chat_feedback` self-
    handle via `ON DELETE SET NULL`.

    "Newest message older than cutoff" is the right boundary because
    `chat_session.time_updated` doesn't get bumped when a new message is
    inserted (the FK is one-way; `onupdate=func.now()` only fires on
    UPDATEs to chat_session itself).

    Batching: we identify session IDs once (the JOIN is the expensive
    part), then delete the dependent + parent rows in chunks of
    `batch_size`, committing between chunks. For dry-run we just count.
    """
    # 1. Find session IDs whose newest message (or session creation, for
    #    sessions with no messages) is older than the cutoff.
    session_id_rows = db_session.execute(
        text(
            """
            SELECT cs.id
            FROM chat_session cs
            LEFT JOIN chat_message cm ON cm.chat_session_id = cs.id
            GROUP BY cs.id
            HAVING COALESCE(MAX(cm.time_sent), cs.time_created) < :cutoff
            """
        ),
        {"cutoff": cutoff},
    ).all()
    session_ids = [r[0] for r in session_id_rows]
    if not session_ids:
        return 0

    if dry_run:
        # Count messages in those sessions (so the user sees both
        # numbers); return session count as the headline.
        msg_count = db_session.execute(
            text(
                "SELECT count(*) FROM chat_message "
                "WHERE chat_session_id = ANY(:ids)"
            ),
            {"ids": session_ids},
        ).scalar() or 0
        # Count search_doc rows that would become orphans (or are already).
        # The query mirrors the orphan-cleanup DELETE at the end of the
        # real path, but bounded to a counter only.
        orphan_count = db_session.execute(
            text(
                "SELECT count(*) FROM search_doc sd "
                "LEFT JOIN chat_message__search_doc cmsd "
                "  ON cmsd.search_doc_id = sd.id "
                "WHERE cmsd.chat_message_id IS NULL"
            )
        ).scalar() or 0
        logger.info(
            f"chat dry-run: {len(session_ids)} sessions would be deleted "
            f"(plus {msg_count} messages, "
            "chat_message__search_doc / tool_call rows, "
            "any chat_message.files LO blobs, and NULLed feedback FKs); "
            f"+ {orphan_count} currently-orphan search_doc rows would be "
            "swept up at the end."
        )
        return len(session_ids)

    total_sessions_deleted = 0
    # Process sessions in batches so each transaction stays small.
    for batch_start in range(0, len(session_ids), batch_size):
        batch_session_ids = session_ids[batch_start : batch_start + batch_size]

        # FK race guard: chat_message.chat_session_id has no `ondelete=`
        # in the schema (defaults to NO ACTION). Without locking, a new
        # chat_message INSERT on one of these sessions between our
        # message-DELETE and session-DELETE would cause the session
        # DELETE to fail with FK violation, rolling back the whole batch.
        # `SELECT ... FOR UPDATE` takes a row-level FOR UPDATE lock,
        # which conflicts with the KEY SHARE lock that any FK-validating
        # INSERT into chat_message must take on its parent chat_session
        # row — so concurrent inserts block until our batch commits.
        # Sessions older than RETENTION_DAYS_CHAT (default 30d) almost
        # never see new activity, so the lock wait is effectively zero.
        db_session.execute(
            text(
                "SELECT id FROM chat_session "
                "WHERE id = ANY(:ids) FOR UPDATE"
            ),
            {"ids": batch_session_ids},
        )

        # Find this batch's message IDs.
        message_id_rows = db_session.execute(
            text(
                "SELECT id FROM chat_message "
                "WHERE chat_session_id = ANY(:ids)"
            ),
            {"ids": batch_session_ids},
        ).all()
        message_ids = [r[0] for r in message_id_rows]

        # Capture file blob names referenced by these messages BEFORE
        # we delete them. The `files` JSONB column holds entries like
        # `{"id": "<file_store.file_name>", ...}`; each one is backed
        # by a `file_store` row + a Postgres LO blob. We unlink them
        # AFTER the batch commit (delete_lobj_by_name commits internally,
        # which would otherwise release our FOR UPDATE locks mid-batch).
        # Mirrors db/chat.py::delete_messages_and_files_from_chat_session.
        blob_names: list[str] = []
        if message_ids:
            files_rows = db_session.execute(
                text(
                    "SELECT files FROM chat_message "
                    "WHERE id = ANY(:ids) AND files IS NOT NULL"
                ),
                {"ids": message_ids},
            ).all()
            for (files_json,) in files_rows:
                for file_info in files_json or []:
                    if not isinstance(file_info, dict):
                        continue
                    name = file_info.get("id")
                    if name:
                        blob_names.append(name)

        if message_ids:
            # 2a. Clean join table — no ondelete on chat_message_id.
            db_session.execute(
                text(
                    "DELETE FROM chat_message__search_doc "
                    "WHERE chat_message_id = ANY(:ids)"
                ),
                {"ids": message_ids},
            )
            # 2b. Clean tool_call — no ondelete on message_id.
            db_session.execute(
                text("DELETE FROM tool_call WHERE message_id = ANY(:ids)"),
                {"ids": message_ids},
            )
            # 2c. Delete chat_message — document_retrieval_feedback and
            #     chat_feedback FKs auto-NULL via ON DELETE SET NULL.
            db_session.execute(
                text("DELETE FROM chat_message WHERE id = ANY(:ids)"),
                {"ids": message_ids},
            )

        # 3. Delete sessions in this batch.
        result = db_session.execute(
            text("DELETE FROM chat_session WHERE id = ANY(:ids)"),
            {"ids": batch_session_ids},
        )
        total_sessions_deleted += int(result.rowcount or 0)

        # Commit per-batch so row-level locks release and other writers
        # can make progress. The retention advisory lock is session-
        # scoped in Postgres, so it survives the commit; the FOR UPDATE
        # locks taken above are released.
        db_session.commit()

        # POST-COMMIT: unlink file blobs. Sessions are now deleted, so
        # FK constraints prevent any new chat_message rows from referring
        # to them — safe to do this outside the batch transaction.
        # delete_lobj_by_name swallows missing-file cases internally.
        for name in blob_names:
            try:
                delete_lobj_by_name(name, db_session)
            except Exception as e:
                logger.warning(
                    f"chat retention: failed to unlink file blob {name!r}: "
                    f"{e}. file_store row may now be orphaned."
                )

    # Clean up search_doc rows that are no longer referenced by any
    # chat_message__search_doc. Mirrors db/chat.py::delete_orphaned_search_docs
    # (called by every UI-driven chat delete) — without this, retention
    # silently leaks search_doc rows. Batched + capped at the same
    # safety ceilings as SQL policies.
    total_orphans = 0
    orphan_hit_ceiling = False
    for _ in range(RETENTION_MAX_BATCHES):
        orphan_result = db_session.execute(
            text(
                "DELETE FROM search_doc WHERE id IN ("
                "  SELECT sd.id FROM search_doc sd "
                "  LEFT JOIN chat_message__search_doc cmsd "
                "    ON cmsd.search_doc_id = sd.id "
                "  WHERE cmsd.chat_message_id IS NULL "
                "  ORDER BY sd.id "
                "  LIMIT :batch_size"
                ")"
            ),
            {"batch_size": batch_size},
        )
        n = int(orphan_result.rowcount or 0)
        db_session.commit()
        total_orphans += n
        if n < batch_size:
            break
    else:
        orphan_hit_ceiling = True

    if total_orphans > 0:
        logger.info(
            f"chat retention: cleaned up {total_orphans} orphan search_doc rows"
        )
    if orphan_hit_ceiling:
        logger.warning(
            f"chat retention: orphan search_doc cleanup hit "
            f"RETENTION_MAX_BATCHES ({RETENTION_MAX_BATCHES}); "
            f"cleaned {total_orphans} this run, remainder picks up next sweep."
        )

    return total_sessions_deleted


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

RETENTION_POLICIES: dict[str, RetentionPolicy] = {
    "kombu_message": RetentionPolicy(
        name="kombu_message",
        days=RETENTION_DAYS_KOMBU,
        count_sql=_KOMBU_COUNT_SQL,
        delete_sql=_KOMBU_DELETE_SQL,
        analyze_table="kombu_message",
    ),
    "task_queue_jobs": RetentionPolicy(
        name="task_queue_jobs",
        days=RETENTION_DAYS_TASK_QUEUE,
        count_sql=_TASK_QUEUE_COUNT_SQL,
        delete_sql=_TASK_QUEUE_DELETE_SQL,
        analyze_table="task_queue_jobs",
    ),
    "index_attempt": RetentionPolicy(
        name="index_attempt",
        days=RETENTION_DAYS_INDEX_ATTEMPT,
        count_sql=_INDEX_ATTEMPT_COUNT_SQL,
        delete_sql=_INDEX_ATTEMPT_DELETE_SQL,
        analyze_table="index_attempt",
    ),
    "permission_sync_run": RetentionPolicy(
        name="permission_sync_run",
        days=RETENTION_DAYS_PERMISSION_SYNC,
        count_sql=_PERMISSION_SYNC_COUNT_SQL,
        delete_sql=_PERMISSION_SYNC_DELETE_SQL,
        analyze_table="permission_sync_run",
    ),
    "usage_reports": RetentionPolicy(
        name="usage_reports",
        days=RETENTION_DAYS_USAGE_REPORTS,
        fn=_delete_old_usage_reports,
    ),
    "chat": RetentionPolicy(
        name="chat",
        days=RETENTION_DAYS_CHAT,
        fn=_delete_old_chat,
    ),
}


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def _try_advisory_lock(db_session: Session) -> bool:
    """Postgres advisory lock — non-blocking. Returns True if acquired."""
    row = db_session.execute(
        text("SELECT pg_try_advisory_lock(:lock_id)"),
        {"lock_id": _RETENTION_ADVISORY_LOCK_ID},
    ).scalar()
    return bool(row)


def _release_advisory_lock(db_session: Session) -> None:
    db_session.execute(
        text("SELECT pg_advisory_unlock(:lock_id)"),
        {"lock_id": _RETENTION_ADVISORY_LOCK_ID},
    )


def run_retention_policies(
    dry_run: bool = False,
    only: list[str] | None = None,
) -> dict[str, int]:
    """Execute every enabled policy. Returns {policy_name: rows_deleted}.

    `dry_run`: prints the row counts that *would* be deleted via cheap
    SELECT count(*) queries — does NOT scan or lock for the DELETE side.
    Useful for the first run after a long period of accumulated bloat.

    `only`: optional list of policy names to run (others skipped). Used
    by `backend/scripts/cleanup_stale_db.py --policy=...`.

    Each policy runs in its own batched loop with per-batch commits, so
    a multi-million-row first run won't hold a table lock for minutes.
    The advisory lock is session-scoped and survives mid-loop commits.
    """
    results: dict[str, int] = {}
    engine = get_sqlalchemy_engine()
    with Session(engine) as db_session:
        if not _try_advisory_lock(db_session):
            logger.warning(
                "Retention: advisory lock held by another run; skipping."
            )
            return results
        try:
            now = datetime.now(tz=timezone.utc)
            for name, policy in RETENTION_POLICIES.items():
                if only and name not in only:
                    continue
                if not policy.enabled:
                    logger.info(f"Retention: {name} disabled, skipping.")
                    continue
                if policy.days <= 0:
                    logger.info(
                        f"Retention: {name} retention=0d, treating as disabled "
                        "(set the corresponding env var to a positive integer "
                        "to enable)."
                    )
                    continue

                cutoff = now - timedelta(days=policy.days)
                started = time.monotonic()
                try:
                    deleted = _run_one(
                        db_session, policy, cutoff, dry_run
                    )
                except Exception as e:
                    logger.exception(
                        f"Retention: {name} failed after "
                        f"{time.monotonic() - started:.2f}s: {e}. "
                        "Rolling back this policy and continuing with the rest."
                    )
                    db_session.rollback()
                    continue

                elapsed = time.monotonic() - started
                results[name] = deleted
                if dry_run:
                    logger.info(
                        f"Retention [DRY RUN]: {name}: would delete {deleted} "
                        f"rows older than {policy.days}d "
                        f"(cutoff {cutoff.isoformat()}, took {elapsed:.2f}s)"
                    )
                elif deleted > 0:
                    logger.info(
                        f"Retention: {name}: deleted {deleted} rows older "
                        f"than {policy.days}d "
                        f"(cutoff {cutoff.isoformat()}, took {elapsed:.2f}s)"
                    )
                else:
                    # No-op runs are common in steady state — keep them at
                    # debug level so daily logs stay quiet. The summary line
                    # in the wrapping Celery task still announces the
                    # overall result.
                    logger.debug(
                        f"Retention: {name}: nothing eligible for deletion "
                        f"(took {elapsed:.2f}s)"
                    )
        finally:
            # Rollback first — if any policy errored out and we're still
            # inside its transaction, the next SQL would raise "current
            # transaction is aborted, commands ignored until end of
            # transaction block", and the unlock would never run. The
            # advisory lock would then sit on this connection forever
            # (or until the connection drops out of the pool), blocking
            # every future retention run with "advisory lock held by
            # another run; skipping". Rollback first puts the session
            # back into a clean state so the unlock can run.
            try:
                db_session.rollback()
            except Exception:
                pass
            try:
                _release_advisory_lock(db_session)
                db_session.commit()
            except Exception:
                logger.exception(
                    "Retention: advisory unlock failed; the lock will be "
                    "released when this DB connection drops out of the "
                    "SQLAlchemy pool. Until then, subsequent retention "
                    "runs on this same connection will skip."
                )
    return results


def _run_one(
    db_session: Session,
    policy: RetentionPolicy,
    cutoff: datetime,
    dry_run: bool,
) -> int:
    """Execute one policy. SQL policies loop their delete_sql in batches of
    RETENTION_BATCH_SIZE, committing between batches. Function policies
    handle their own batching."""
    # Function-based policies (chat) — they handle batching + commits
    # internally and obey dry-run themselves.
    if policy.fn is not None:
        return policy.fn(db_session, cutoff, RETENTION_BATCH_SIZE, dry_run)

    assert policy.count_sql is not None and policy.delete_sql is not None

    params: dict = {"cutoff": cutoff, "batch_size": RETENTION_BATCH_SIZE}
    if policy.name == "index_attempt":
        params["keep_n"] = RETENTION_KEEP_LAST_N_INDEX_ATTEMPTS

    # Dry-run: cheap SELECT count, no DELETE scan, no locks.
    if dry_run:
        count_params = {k: v for k, v in params.items() if k != "batch_size"}
        n = db_session.execute(text(policy.count_sql), count_params).scalar() or 0
        return int(n)

    # Real run: loop DELETE … LIMIT :batch_size until empty (or the safety
    # ceiling RETENTION_MAX_BATCHES is hit). Commit per batch so locks
    # release and concurrent writers aren't blocked.
    total = 0
    hit_ceiling = False
    for i in range(RETENTION_MAX_BATCHES):
        result = db_session.execute(text(policy.delete_sql), params)
        n = int(result.rowcount or 0)
        db_session.commit()
        total += n
        if n < RETENTION_BATCH_SIZE:
            # Last batch was partial → no more rows match → done.
            break
        logger.debug(
            f"Retention: {policy.name}: batch {i + 1} deleted {n} rows "
            f"(running total: {total})"
        )
    else:
        hit_ceiling = True

    if hit_ceiling:
        logger.warning(
            f"Retention: {policy.name}: hit RETENTION_MAX_BATCHES "
            f"({RETENTION_MAX_BATCHES}); deleted {total} rows this run, "
            "remainder will be picked up by the next scheduled sweep. "
            "If this happens persistently, raise RETENTION_MAX_BATCHES "
            "or run cleanup_stale_db.py manually."
        )

    # Refresh planner stats after substantial purges. Threshold = one full
    # batch — small steady-state cleanups don't need it. ANALYZE doesn't
    # take heavy locks (just SHARE UPDATE EXCLUSIVE on stats), but it is
    # I/O, so we gate on volume.
    if policy.analyze_table and total >= RETENTION_BATCH_SIZE:
        try:
            db_session.execute(text(f"ANALYZE {policy.analyze_table}"))
            db_session.commit()
            logger.info(
                f"Retention: {policy.name}: ran ANALYZE "
                f"{policy.analyze_table} after deleting {total} rows"
            )
        except Exception as e:
            logger.warning(
                f"Retention: {policy.name}: ANALYZE {policy.analyze_table} "
                f"failed: {e}. Stats will refresh on next autovacuum cycle."
            )
            db_session.rollback()

    return total
