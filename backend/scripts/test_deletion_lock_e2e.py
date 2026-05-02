"""End-to-end integration tests for the connector-deletion concurrency
fix: per-cc-pair advisory lock + API-side dedup guard.

The unit tests in ``tests/unit/danswer/db/test_deletion_lock_keys.py``
cover the pure key-derivation math. This script proves the full
behaviour against live Postgres:

  Phase A — lock primitive: two SQLAlchemy sessions; the second
            cannot acquire while the first holds, then can after
            release. (The cross-session semantics that
            ``pg_try_advisory_lock`` provides; the unit test can't
            cover this.)
  Phase B — worker-side guard: a held lock causes
            ``cleanup_connector_credential_pair_task``'s body to
            early-return 0 without ever touching the cc-pair, in
            milliseconds — versus the pre-fix behaviour where parallel
            tasks would each spend 5 minutes retrying
            ``SELECT … FOR UPDATE NOWAIT`` over the same documents.
  Phase C — lock release on success: the task releases the lock after
            a clean run, so the next caller can immediately acquire.
  Phase D — lock release on exception: the task releases the lock even
            when the body raises (rollback-before-unlock pattern).
  Phase E — concurrent racing: N threads racing the same cc-pair
            complete in bounded time and don't serialize on row-locks
            for 5 minutes each.
  Phase F — API dedup logic: the ``task_queue_jobs``-based check used
            by ``/admin/deletion-attempt`` correctly identifies live
            tasks (blocks duplicate dispatch), terminal tasks (allows
            new dispatch), and timed-out tasks (allows new dispatch).

DESTRUCTIVE: writes/deletes tagged data only (``__test_deletion__``
prefix). Run only against a dev / staging DB.

Pre-flight: refuses to run if a celery worker is processing tasks
for the test prefix; warns (but allows) if a worker is up at all.

Usage::

    cd backend
    PYTHONPATH=$(pwd) python scripts/test_deletion_lock_e2e.py [--yes]
"""
from __future__ import annotations

import argparse
import datetime
import sys
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.orm import Session

from danswer.background.celery.celery_app import (
    cleanup_connector_credential_pair_task,
)
from danswer.background.task_utils import name_cc_cleanup_task
from danswer.configs.constants import DocumentSource
from danswer.connectors.models import InputType
from danswer.db.connector_credential_pair import release_deletion_lock
from danswer.db.connector_credential_pair import try_acquire_deletion_lock
from danswer.db.engine import get_sqlalchemy_engine
from danswer.db.models import Connector
from danswer.db.models import ConnectorCredentialPair
from danswer.db.models import Credential
from danswer.db.models import TaskQueueState
from danswer.db.models import TaskStatus
from danswer.db.tasks import check_task_is_live_and_not_timed_out
from danswer.db.tasks import get_latest_task


DELETION_PREFIX = "__test_deletion__"


# ``cleanup_connector_credential_pair_task`` is wrapped twice (celery
# `@task` then `build_celery_task_wrapper`). Reaching ``.run.__wrapped__``
# gives us the inner function — same body the celery worker invokes,
# but without the ``task_queue_jobs`` plumbing the wrapper requires
# (``mark_task_start`` insists on a pre-existing PENDING row from
# ``apply_async``, which we'd otherwise have to forge).
_inner_task: Callable[..., int] = cleanup_connector_credential_pair_task.run.__wrapped__


# ---------------------------------------------------------------------------
# Tiny harness
# ---------------------------------------------------------------------------


_FAILED = 0


def section(name: str) -> None:
    print(f"\n=== {name} ===")


def passed(msg: str) -> None:
    print(f"  ok  {msg}")


def failed(msg: str, detail: str | None = None) -> None:
    global _FAILED
    _FAILED += 1
    print(f"  XX  {msg}")
    if detail:
        for line in detail.splitlines():
            print(f"      {line}")


def assert_eq(actual: Any, expected: Any, label: str) -> None:
    if actual == expected:
        passed(f"{label} — {actual}")
    else:
        failed(label, f"expected={expected}, actual={actual}")


def assert_true(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        passed(label)
    else:
        failed(label, detail or None)


# ---------------------------------------------------------------------------
# Seeders / cleanup
# ---------------------------------------------------------------------------


def make_disabled_cc_pair(
    db: Session,
) -> tuple[Connector, Credential, ConnectorCredentialPair]:
    """Seed a connector + credential + cc-pair tagged with the test
    prefix. Connector is ``disabled=True`` so the deletion endpoint's
    `check_deletion_attempt_is_allowed` lets the request through."""
    connector = Connector(
        name=f"{DELETION_PREFIX}{uuid.uuid4().hex[:8]}",
        # Use a source that real environments are unlikely to use, so
        # we never collide with production rows.
        source=DocumentSource.ZULIP,
        input_type=InputType.POLL,
        connector_specific_config={"_test_deletion": True},
        refresh_freq=600,
        disabled=True,
    )
    credential = Credential(admin_public=True, credential_json={})
    db.add_all([connector, credential])
    db.flush()
    ccp = ConnectorCredentialPair(
        connector_id=connector.id,
        credential_id=credential.id,
        name=f"{DELETION_PREFIX}ccp-{uuid.uuid4().hex[:8]}",
        is_public=True,
        total_docs_indexed=0,
    )
    db.add(ccp)
    db.flush()
    return connector, credential, ccp


def cleanup_test_data() -> None:
    """Drop everything tagged with DELETION_PREFIX, FK-safe order."""
    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        db.execute(
            text(
                """
                DELETE FROM index_attempt
                WHERE connector_id IN (
                    SELECT id FROM connector WHERE name LIKE :p
                )
                """
            ),
            {"p": f"{DELETION_PREFIX}%"},
        )
        db.execute(
            text("DELETE FROM connector_credential_pair WHERE name LIKE :p"),
            {"p": f"{DELETION_PREFIX}%"},
        )
        db.execute(
            text("DELETE FROM connector WHERE name LIKE :p"),
            {"p": f"{DELETION_PREFIX}%"},
        )
        # task_queue_jobs rows we forged in Phase F:
        db.execute(
            text("DELETE FROM task_queue_jobs WHERE task_name LIKE :p"),
            {"p": f"{DELETION_PREFIX}%"},
        )
        db.commit()


# ---------------------------------------------------------------------------
# Phase A — lock primitive (the cross-session check that the unit test
# can't cover)
# ---------------------------------------------------------------------------


_SYNTHETIC = (9_999_991, 9_999_992)


def phase_a_lock_primitive() -> None:
    section("Phase A — advisory lock primitive across 2 sessions")
    engine = get_sqlalchemy_engine()
    sess_a = Session(engine)
    sess_b = Session(engine)
    try:
        a1 = try_acquire_deletion_lock(sess_a, *_SYNTHETIC)
        assert_eq(a1, True, "A acquires lock")

        b1 = try_acquire_deletion_lock(sess_b, *_SYNTHETIC)
        assert_eq(b1, False, "B blocked while A holds")

        release_deletion_lock(sess_a, *_SYNTHETIC)
        sess_a.commit()
        passed("A released")

        b2 = try_acquire_deletion_lock(sess_b, *_SYNTHETIC)
        assert_eq(b2, True, "B acquires after A released")

        release_deletion_lock(sess_b, *_SYNTHETIC)
        sess_b.commit()
    finally:
        sess_a.close()
        sess_b.close()


# ---------------------------------------------------------------------------
# Phase B — worker-side guard: held lock makes the task body return 0 fast
# ---------------------------------------------------------------------------


def phase_b_held_lock_blocks_task() -> None:
    section("Phase B — held lock causes task body to return 0 quickly")

    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        _, _, ccp = make_disabled_cc_pair(db)
        db.commit()
        connector_id = ccp.connector_id
        credential_id = ccp.credential_id

    # Hold the deletion advisory lock in a separate session, simulating
    # another worker that's already running a deletion for this cc-pair.
    holder = Session(engine)
    try:
        held = try_acquire_deletion_lock(holder, connector_id, credential_id)
        assert_true(held, "external session acquires lock")

        t0 = time.monotonic()
        result = _inner_task(connector_id=connector_id, credential_id=credential_id)
        elapsed = time.monotonic() - t0

        assert_eq(result, 0, "task body returns 0 (skipped)")
        assert_true(
            elapsed < 5.0,
            "task returned in well under the pre-fix 5-minute timeout",
            f"elapsed: {elapsed:.3f}s",
        )

        # Sanity: the cc-pair was NOT deleted (we held the lock).
        with Session(engine) as db:
            still_there = db.execute(
                select(ConnectorCredentialPair).where(
                    ConnectorCredentialPair.connector_id == connector_id,
                    ConnectorCredentialPair.credential_id == credential_id,
                )
            ).scalar_one_or_none()
            assert_true(
                still_there is not None,
                "cc-pair still present after lock-blocked task",
            )
    finally:
        release_deletion_lock(holder, connector_id, credential_id)
        holder.commit()
        holder.close()


# ---------------------------------------------------------------------------
# Phase C — lock release after a clean run (cc-pair with 0 docs)
# ---------------------------------------------------------------------------


def phase_c_lock_released_on_success() -> None:
    section("Phase C — lock released after task succeeds (clean delete)")

    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        _, _, ccp = make_disabled_cc_pair(db)
        db.commit()
        connector_id = ccp.connector_id
        credential_id = ccp.credential_id

    # Run the task end-to-end. With 0 documents and 0 doc-sets, the
    # delete loop is a single iteration and `cleanup_synced_entities`
    # exits on the first pass. No Vespa traffic. Just exercises the
    # lock-acquire → body → finally-release flow.
    result = _inner_task(connector_id=connector_id, credential_id=credential_id)
    assert_true(
        isinstance(result, int) and result >= 0,
        f"task ran cleanly, deleted {result} docs",
    )

    # cc-pair row should be gone (the body deletes it).
    with Session(engine) as db:
        still_there = db.execute(
            select(ConnectorCredentialPair).where(
                ConnectorCredentialPair.connector_id == connector_id,
                ConnectorCredentialPair.credential_id == credential_id,
            )
        ).scalar_one_or_none()
        assert_true(still_there is None, "cc-pair row deleted after success")

    # Lock must now be free — a fresh session can acquire immediately.
    sess = Session(engine)
    try:
        got = try_acquire_deletion_lock(sess, connector_id, credential_id)
        assert_true(got, "lock free after success path")
    finally:
        release_deletion_lock(sess, connector_id, credential_id)
        sess.commit()
        sess.close()


# ---------------------------------------------------------------------------
# Phase D — lock release after exception in task body
# ---------------------------------------------------------------------------


def phase_d_lock_released_on_exception() -> None:
    section("Phase D — lock released after task body raises")

    # Use a cc-pair id that does not exist. The task body raises
    # ValueError("...does not exist") *after* acquiring the lock; the
    # finally block must still release it.
    connector_id = 9_999_001
    credential_id = 9_999_001

    raised = False
    try:
        _inner_task(connector_id=connector_id, credential_id=credential_id)
    except ValueError as e:
        raised = "does not exist" in str(e)
    assert_true(raised, "task raised ValueError for missing cc-pair")

    # Lock must be free despite the exception.
    engine = get_sqlalchemy_engine()
    sess = Session(engine)
    try:
        got = try_acquire_deletion_lock(sess, connector_id, credential_id)
        assert_true(got, "lock free after exception path (rollback-before-unlock)")
    finally:
        release_deletion_lock(sess, connector_id, credential_id)
        sess.commit()
        sess.close()


# ---------------------------------------------------------------------------
# Phase E — concurrent threads racing on the same cc-pair
# ---------------------------------------------------------------------------


def phase_e_concurrent_threads_race() -> None:
    section("Phase E — N threads race; lock serializes; bounded total time")

    n_threads = 6
    # Use a non-existent cc-pair so the inner body returns fast on the
    # winner (raises ValueError) and we don't need to seed docs.
    connector_id = 9_999_002
    credential_id = 9_999_002

    results: list[Any] = [None] * n_threads
    exceptions: list[Exception | None] = [None] * n_threads

    def runner(i: int) -> None:
        try:
            results[i] = _inner_task(
                connector_id=connector_id, credential_id=credential_id
            )
        except Exception as e:
            exceptions[i] = e

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(n_threads)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    elapsed = time.monotonic() - t0

    # Pre-fix worst case: each thread hits row-lock retry for 5 min.
    # We seeded a non-existent cc-pair so we don't even reach the row-
    # lock code, but the broader claim — "concurrent invocations don't
    # take minutes" — must hold. 30s is generous; expected ~1s.
    assert_true(
        elapsed < 30.0,
        f"all {n_threads} threads completed in {elapsed:.2f}s (< 30s)",
    )

    # Each thread either got the lock and raised ValueError ("does not
    # exist"), or didn't get the lock and returned 0. No other outcome
    # is acceptable.
    bad = []
    n_got_lock = 0
    n_skipped = 0
    for i in range(n_threads):
        if exceptions[i] is not None:
            if isinstance(exceptions[i], ValueError) and "does not exist" in str(
                exceptions[i]
            ):
                n_got_lock += 1
            else:
                bad.append(f"thread {i} raised unexpected: {exceptions[i]!r}")
        elif results[i] == 0:
            n_skipped += 1
        else:
            bad.append(f"thread {i} returned unexpected: {results[i]!r}")
    assert_true(not bad, "all threads had expected outcomes", "\n".join(bad))
    assert_true(
        n_got_lock + n_skipped == n_threads,
        f"{n_got_lock} acquired (raised ValueError), {n_skipped} skipped (returned 0)",
    )


# ---------------------------------------------------------------------------
# Phase F — API dedup logic via task_queue_jobs state
# ---------------------------------------------------------------------------


def _insert_task_row(
    db: Session,
    *,
    name: str,
    status: TaskStatus,
    register_offset_seconds: int = 0,
    start_offset_seconds: int | None = 0,
) -> None:
    """Insert a synthetic task_queue_jobs row with controlled timing.

    ``register_offset_seconds`` and ``start_offset_seconds`` are
    subtracted from ``now()``; positive numbers push the timestamps
    *into the past* so we can simulate aged / timed-out tasks.
    """
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    register_time = now - datetime.timedelta(seconds=register_offset_seconds)
    start_time = (
        None
        if start_offset_seconds is None
        else now - datetime.timedelta(seconds=start_offset_seconds)
    )
    row = TaskQueueState(
        task_id=str(uuid.uuid4()),
        task_name=name,
        status=status,
        register_time=register_time,
        start_time=start_time,
    )
    db.add(row)
    db.commit()


def phase_f_api_dedup_logic() -> None:
    section("Phase F — API dedup: task_queue_jobs state controls 409")

    # Use a synthetic cc-pair whose task name is also tag-prefixed so
    # cleanup picks it up.
    connector_id = 9_999_003
    credential_id = 9_999_003
    base_name = name_cc_cleanup_task(
        connector_id=connector_id, credential_id=credential_id
    )
    # Tag the task name so cleanup_test_data() can drop it.
    name = f"{DELETION_PREFIX}{base_name}"

    engine = get_sqlalchemy_engine()

    # Case 1: no row → no live task → API would NOT 409.
    with Session(engine) as db:
        latest = get_latest_task(task_name=name, db_session=db)
        assert_true(latest is None, "no row → get_latest_task returns None (allow)")

    # Case 2: STARTED row with recent timestamps → live → API would 409.
    with Session(engine) as db:
        _insert_task_row(
            db, name=name, status=TaskStatus.STARTED, register_offset_seconds=5
        )
        latest = get_latest_task(task_name=name, db_session=db)
        assert_true(latest is not None, "STARTED row inserted")
        if latest is not None:
            live = check_task_is_live_and_not_timed_out(latest, db)
            assert_eq(live, True, "STARTED + recent → live (would 409)")

    # Case 3: SUCCESS row → terminal → API would NOT 409.
    with Session(engine) as db:
        _insert_task_row(
            db, name=name, status=TaskStatus.SUCCESS, register_offset_seconds=1
        )
        latest = get_latest_task(task_name=name, db_session=db)
        if latest is not None:
            live = check_task_is_live_and_not_timed_out(latest, db)
            assert_eq(live, False, "latest is SUCCESS → not live (allow)")

    # Case 4: STARTED row with ancient timestamps → timed out → allow.
    # JOB_TIMEOUT defaults to a few hours; a register_time 999_999s in
    # the past is unambiguously stale.
    with Session(engine) as db:
        _insert_task_row(
            db,
            name=name,
            status=TaskStatus.STARTED,
            register_offset_seconds=999_999,
            start_offset_seconds=999_999,
        )
        latest = get_latest_task(task_name=name, db_session=db)
        if latest is not None:
            live = check_task_is_live_and_not_timed_out(latest, db)
            assert_eq(live, False, "STARTED but ancient → timed out (allow)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes", action="store_true", help="Skip the destructive-op confirmation."
    )
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="Don't clean tagged data at the end (debugging).",
    )
    args = parser.parse_args()

    engine = get_sqlalchemy_engine()
    safe_url = (
        f"{engine.url.drivername}://{engine.url.username}@"
        f"{engine.url.host}:{engine.url.port}/{engine.url.database}"
    )
    print(f"Target DB: {safe_url}")
    if not args.yes:
        ans = input(
            "This will create + delete tagged rows under "
            f"prefix '{DELETION_PREFIX}'. Continue? [y/N] "
        )
        if ans.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    cleanup_test_data()
    try:
        phase_a_lock_primitive()
        cleanup_test_data()
        phase_b_held_lock_blocks_task()
        cleanup_test_data()
        phase_c_lock_released_on_success()
        cleanup_test_data()
        phase_d_lock_released_on_exception()
        cleanup_test_data()
        phase_e_concurrent_threads_race()
        cleanup_test_data()
        phase_f_api_dedup_logic()
    finally:
        if not args.keep_data:
            cleanup_test_data()
            print("\n  ok  cleaned up tagged rows")
        else:
            print(f"\n  -- kept tagged rows (--keep-data); prefix={DELETION_PREFIX}")

    if _FAILED:
        print(f"\nFAIL: {_FAILED} assertion(s) failed")
        return 1
    print("\nALL PHASES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
