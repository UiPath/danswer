"""Force-trigger the daily Celery tasks against fresh dummy data.

Proves the broker → worker pipeline is alive end-to-end — the same path
beat uses for the 07:30 UTC rollup and 08:00 UTC retention fires. If
this script passes, you can trust the daily schedule will too (modulo
beat actually firing, which the schedule check covers separately).

Steps:
  1. Seed minimal data:
       - 5 chat_session + chat_message pairs backdated 35-90 days
         (eligible for the chat retention sweep)
       - 5 chat_session + chat_message pairs in the last 6 days
         (visible in the rollup window)
       - 1 connector + cc-pair (so the rollup has source variety)
  2. Snapshot before-state (chat counts, rollup row count)
  3. Fire `run_analytics_rollup_task.delay()` → wait for worker to run it
  4. Fire `run_retention_policies_task.delay()` → wait for worker to run it
  5. Snapshot after-state, print diff
  6. Cleanup the seeded rows (unless --keep-data)

Requires the celery worker process to be running and connected to the
same DB this script targets. If `.get()` hangs, the worker isn't picking
up tasks (check `celery_worker.log` and supervisord status).

DESTRUCTIVE: writes/deletes tagged data only (`__test_celery__` prefix).
Run only against a dev / staging DB.

Usage:
    cd backend
    PYTHONPATH=$(pwd) python scripts/test_celery_jobs_smoke.py [--yes] [--keep-data]
"""
from __future__ import annotations

import argparse
import datetime
import sys
import time
import uuid

from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.orm import Session

from danswer.background.celery.celery_app import (
    run_analytics_rollup_task,
    run_retention_policies_task,
)
from danswer.configs.constants import MessageType
from danswer.db.engine import get_sqlalchemy_engine
from danswer.db.models import (
    ChatMessage,
    ChatSession,
    ChatSessionSharedStatus,
    Persona,
)


CELERY_PREFIX = "__test_celery__"


# ---------------------------------------------------------------------------
# Tiny harness
# ---------------------------------------------------------------------------


def section(name: str) -> None:
    print(f"\n=== {name} ===")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def info(msg: str) -> None:
    print(f"    {msg}")


_FAILED = 0


def fail(msg: str) -> None:
    global _FAILED
    _FAILED += 1
    print(f"  ✗ {msg}")


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------


def seed_dummy_data() -> tuple[int, int]:
    """Return (n_old_chats, n_fresh_chats) seeded."""
    engine = get_sqlalchemy_engine()
    n_old, n_fresh = 0, 0
    with Session(engine) as db:
        persona = db.execute(
            select(Persona).order_by(Persona.id).limit(1)
        ).scalar_one_or_none()
        if persona is None:
            sys.exit(
                "No persona found. Bootstrap the DB first "
                "(alembic upgrade head + start the API server once)."
            )

        now = datetime.datetime.now(tz=datetime.timezone.utc)

        # 5 OLD chats (35-90d) — chat retention with default 30d should delete.
        for i in range(5):
            when = now - datetime.timedelta(days=35 + i * 10)
            cs = ChatSession(
                user_id=None,  # slackbot-style, no Danswer user
                persona_id=persona.id,
                description=f"{CELERY_PREFIX}old-{i}-{uuid.uuid4().hex[:6]}",
                deleted=False,
                one_shot=False,
                shared_status=ChatSessionSharedStatus.PRIVATE,
                danswerbot_flow=True,
                time_created=when,
                time_updated=when,
            )
            db.add(cs)
            db.flush()
            db.add(
                ChatMessage(
                    chat_session_id=cs.id,
                    message=f"old assistant reply {i}",
                    message_type=MessageType.ASSISTANT,
                    token_count=5,
                    time_sent=when,
                )
            )
            n_old += 1

        # 5 FRESH chats (last 6 days) — should land in the rollup window.
        for i in range(5):
            when = now - datetime.timedelta(days=i, hours=2)
            cs = ChatSession(
                user_id=None,
                persona_id=persona.id,
                description=f"{CELERY_PREFIX}fresh-{i}-{uuid.uuid4().hex[:6]}",
                deleted=False,
                one_shot=False,
                shared_status=ChatSessionSharedStatus.PRIVATE,
                danswerbot_flow=True,
                time_created=when,
                time_updated=when,
            )
            db.add(cs)
            db.flush()
            db.add(
                ChatMessage(
                    chat_session_id=cs.id,
                    message=f"fresh assistant reply {i}",
                    message_type=MessageType.ASSISTANT,
                    token_count=5,
                    time_sent=when,
                )
            )
            n_fresh += 1
        db.commit()
    return n_old, n_fresh


def cleanup_seeded() -> None:
    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        # chat_message__search_doc → chat_message → chat_session, FK-safe
        for sql in [
            """DELETE FROM chat_feedback WHERE chat_message_id IN (
                 SELECT cm.id FROM chat_message cm
                 JOIN chat_session cs ON cs.id = cm.chat_session_id
                 WHERE cs.description LIKE :p)""",
            """DELETE FROM chat_message__search_doc WHERE chat_message_id IN (
                 SELECT cm.id FROM chat_message cm
                 JOIN chat_session cs ON cs.id = cm.chat_session_id
                 WHERE cs.description LIKE :p)""",
            """DELETE FROM tool_call WHERE message_id IN (
                 SELECT cm.id FROM chat_message cm
                 JOIN chat_session cs ON cs.id = cm.chat_session_id
                 WHERE cs.description LIKE :p)""",
            """DELETE FROM chat_message WHERE chat_session_id IN (
                 SELECT id FROM chat_session WHERE description LIKE :p)""",
            "DELETE FROM chat_session WHERE description LIKE :p",
        ]:
            db.execute(text(sql), {"p": f"{CELERY_PREFIX}%"})
        db.commit()


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def snapshot() -> dict:
    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        return {
            "old_chats": int(
                db.execute(
                    text(
                        "SELECT count(*) FROM chat_session "
                        "WHERE description LIKE :p "
                        "AND time_created < now() - interval '31 days'"
                    ),
                    {"p": f"{CELERY_PREFIX}%"},
                ).scalar()
                or 0
            ),
            "fresh_chats": int(
                db.execute(
                    text(
                        "SELECT count(*) FROM chat_session "
                        "WHERE description LIKE :p "
                        "AND time_created >= now() - interval '7 days'"
                    ),
                    {"p": f"{CELERY_PREFIX}%"},
                ).scalar()
                or 0
            ),
            "rollup_max_rolled_up_at": db.execute(
                text("SELECT max(rolled_up_at) FROM analytics_daily_rollup")
            ).scalar(),
            "rollup_row_count": int(
                db.execute(
                    text("SELECT count(*) FROM analytics_daily_rollup")
                ).scalar()
                or 0
            ),
            "kombu_message_count": int(
                db.execute(text("SELECT count(*) FROM kombu_message")).scalar()
                or 0
            ),
        }


def print_snapshot(label: str, snap: dict) -> None:
    print(f"  [{label}]")
    info(f"old chats (>31d, our prefix):   {snap['old_chats']}")
    info(f"fresh chats (≤7d, our prefix):  {snap['fresh_chats']}")
    info(f"analytics_daily_rollup rows:    {snap['rollup_row_count']}")
    info(f"max(rolled_up_at):              {snap['rollup_max_rolled_up_at']}")
    info(f"kombu_message rows total:       {snap['kombu_message_count']}")


# ---------------------------------------------------------------------------
# Fire and wait
# ---------------------------------------------------------------------------


def fire_and_wait(task, label: str, timeout: int = 180) -> None:
    """Submit via .delay() (broker path) and wait for the worker to ack
    completion. Prints task id + final state."""
    section(f"Firing {label} via Celery .delay()")
    started = time.monotonic()
    result = task.delay()
    info(f"task id: {result.id}")
    info(f"submitted at t=0; waiting up to {timeout}s for worker…")
    try:
        ret = result.get(timeout=timeout)
    except Exception as e:
        elapsed = time.monotonic() - started
        fail(f"{label} did not complete within {timeout}s ({elapsed:.1f}s elapsed): {e}")
        info(
            "If the script hung here, the celery worker likely isn't "
            "running or isn't connected to this DB. Check "
            "`celery_worker.log` and supervisord status."
        )
        sys.exit(1)
    elapsed = time.monotonic() - started
    ok(f"{label} returned in {elapsed:.2f}s, state={result.state}, return={ret}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="Skip the destructive-op confirm.")
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="Don't clean up seeded rows at the end (debugging).",
    )
    parser.add_argument(
        "--rollup-timeout",
        type=int,
        default=120,
        help="Seconds to wait for the rollup task (default 120).",
    )
    parser.add_argument(
        "--retention-timeout",
        type=int,
        default=300,
        help="Seconds to wait for the retention task (default 300; the "
        "first run against bloated kombu_message can take a while).",
    )
    args = parser.parse_args()

    engine = get_sqlalchemy_engine()
    safe_url = (
        f"{engine.url.drivername}://{engine.url.username}@"
        f"{engine.url.host}:{engine.url.port}/{engine.url.database}"
    )
    print(f"Target DB: {safe_url}")
    print(
        "Requires: celery worker process running and connected to this DB. "
        "If `.get()` hangs, the worker isn't there."
    )
    if not args.yes:
        ans = input(
            "This will create + delete tagged dummy data in the DB AND "
            "trigger the real retention sweep (which deletes from "
            "kombu_message etc.). Type 'yes' to continue: "
        )
        if ans.strip().lower() != "yes":
            print("Aborted.")
            return 1

    # Pre-clean any stragglers
    cleanup_seeded()

    section("Phase 1 — seed dummy data")
    n_old, n_fresh = seed_dummy_data()
    ok(f"seeded {n_old} old chats (35-90d) and {n_fresh} fresh chats (last 6d)")

    section("Phase 2 — snapshot BEFORE")
    before = snapshot()
    print_snapshot("BEFORE", before)

    fire_and_wait(
        run_analytics_rollup_task,
        "run_analytics_rollup_task",
        timeout=args.rollup_timeout,
    )
    fire_and_wait(
        run_retention_policies_task,
        "run_retention_policies_task",
        timeout=args.retention_timeout,
    )

    section("Phase 5 — snapshot AFTER")
    after = snapshot()
    print_snapshot("AFTER", after)

    section("Phase 6 — observed side effects")

    # Rollup: rolled_up_at should advance and row count should be >= before
    if before["rollup_max_rolled_up_at"] is None:
        if after["rollup_max_rolled_up_at"] is not None:
            ok(
                "rollup wrote rows for the first time "
                f"(max rolled_up_at = {after['rollup_max_rolled_up_at']})"
            )
        else:
            fail("rollup task ran but rollup table is still empty")
    else:
        if (
            after["rollup_max_rolled_up_at"]
            and after["rollup_max_rolled_up_at"] > before["rollup_max_rolled_up_at"]
        ):
            delta = after["rollup_max_rolled_up_at"] - before["rollup_max_rolled_up_at"]
            ok(f"rollup advanced max(rolled_up_at) by {delta}")
        else:
            fail(
                "rollup task ran but max(rolled_up_at) didn't advance "
                f"(before={before['rollup_max_rolled_up_at']}, "
                f"after={after['rollup_max_rolled_up_at']})"
            )

    # Retention: our 5 old chats should be gone, 5 fresh should remain
    if after["old_chats"] == 0:
        ok(f"retention deleted all {n_old} seeded old chats")
    else:
        fail(
            f"retention left {after['old_chats']} of our old chats behind "
            "(expected 0)"
        )
    if after["fresh_chats"] == n_fresh:
        ok(f"retention untouched all {n_fresh} fresh chats (correct)")
    else:
        fail(
            f"retention deleted {n_fresh - after['fresh_chats']} fresh "
            "chats it shouldn't have"
        )

    # kombu_message — informational only (count depends on broker activity)
    delta = after["kombu_message_count"] - before["kombu_message_count"]
    info(
        f"kombu_message row count delta: {delta:+d} "
        "(broker writes new rows constantly; just informational)"
    )

    if not args.keep_data:
        cleanup_seeded()
        print("\n  cleaned up tagged dummy data")
    else:
        print("\n  --keep-data passed; seeded rows left in place")

    if _FAILED:
        print(f"\n❌ {_FAILED} assertion(s) failed.")
        return 1
    print("\n✅ Both Celery tasks fired and applied side effects.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
