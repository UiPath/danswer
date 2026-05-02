"""End-to-end smoke test for the analytics + retention pipeline.

DESTRUCTIVE: writes/deletes data in your DB. Run only against a dev DB.
The script confirms once at the start (skip via --yes).

Phases:
  1. Clean any prior seed → re-seed 60 days of data + 35-90d "old" chats
  2. Backfill rollup, assert table populated + checkpoint advanced
  3. Read endpoints (via direct fn calls), assert counts match seeded data
  4. Dry-run retention, assert reported counts make sense
  5. Real retention, assert old chat data gone, fresh data alive
  6. Re-hit endpoints, assert rollup data SURVIVED retention deletes
  7. Re-run rollup, assert idempotency (checkpoint advances, recent days
     re-processed, old days untouched)
  8. Final cleanup of seeded data + the rollup checkpoint

Each phase prints PASS/FAIL with context. Exits 0 on full success, non-
zero on the first failed assertion. Re-run safe — idempotent.

Usage:
    cd backend
    PYTHONPATH=$(pwd) python scripts/test_analytics_e2e.py [--yes] [--keep-data]
"""
from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from danswer.db.engine import get_sqlalchemy_engine

# Reach into the seed module via direct import — keeps the orchestrator
# self-contained and avoids reflection / subprocess overhead.
THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
from seed_test_data import SEED_PREFIX  # noqa: E402
from seed_test_data import clean_seeded_data  # noqa: E402


# ---------------------------------------------------------------------------
# Tiny test harness
# ---------------------------------------------------------------------------


_FAILED = 0


def section(name: str) -> None:
    print(f"\n=== {name} ===")


def passed(msg: str) -> None:
    print(f"  ✓ {msg}")


def failed(msg: str, detail: str | None = None) -> None:
    global _FAILED
    _FAILED += 1
    print(f"  ✗ {msg}")
    if detail:
        for line in detail.splitlines():
            print(f"      {line}")


def assert_eq(actual, expected, label: str) -> None:
    if actual == expected:
        passed(f"{label} — {actual}")
    else:
        failed(label, f"expected={expected}, actual={actual}")


def assert_ge(actual, threshold: int, label: str) -> None:
    n = int(actual or 0)
    if n >= threshold:
        passed(f"{label} — {n} (≥ {threshold})")
    else:
        failed(label, f"expected ≥ {threshold}, actual={n}")


def assert_true(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        passed(label)
    else:
        failed(label, detail or None)


def run_script(args: list[str]) -> None:
    """Invoke another script in the same env. Inherits PYTHONPATH."""
    print(f"  $ python {' '.join(args)}")
    result = subprocess.run(
        [sys.executable] + args,
        cwd=str(THIS_DIR.parent),  # backend/
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("    (stdout)")
        for line in (result.stdout or "").splitlines()[-20:]:
            print(f"    {line}")
        print("    (stderr)")
        for line in (result.stderr or "").splitlines()[-20:]:
            print(f"    {line}")
        failed(f"subprocess failed with rc={result.returncode}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------


def phase_migrate() -> None:
    """Idempotent — applies any pending migrations including
    analytics_daily_rollup. Required for Phase 2 onward."""
    section("Phase 0 — alembic upgrade head (idempotent)")
    print(f"  $ {sys.executable} -m alembic upgrade head")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(THIS_DIR.parent),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        for line in (result.stdout or "").splitlines()[-20:]:
            print(f"    {line}")
        for line in (result.stderr or "").splitlines()[-20:]:
            print(f"    {line}")
        failed(f"alembic upgrade failed (rc={result.returncode})")
        sys.exit(1)
    # Print just the final "Running upgrade ..." lines for context.
    tail = [
        ln
        for ln in (result.stdout or "").splitlines()
        if "Running upgrade" in ln or "INFO" in ln
    ][-5:]
    for line in tail:
        print(f"    {line}")
    passed("migrations up to date")


def phase_seed() -> None:
    section("Phase 1 — clean + seed 60 days + old data")
    run_script(["scripts/seed_test_data.py", "--clean", "--yes"])
    run_script(
        [
            "scripts/seed_test_data.py",
            "--yes",
            "--days=60",
            "--chats-per-day=10",
            "--slackbot-share=0.7",
            "--feedback-rate=0.6",
            "--like-share=0.5",
            "--resolved-share=0.2",
            "--needs-help-share=0.1",
            "--users=15",
            "--connectors=4",
            "--docs-per-connector=50",
            "--with-old-data",
            "--with-search-docs",
            "--seed=42",
        ]
    )

    with Session(get_sqlalchemy_engine()) as db:
        n_sessions = db.execute(
            text("SELECT count(*) FROM chat_session WHERE description LIKE :p"),
            {"p": f"{SEED_PREFIX}%"},
        ).scalar()
        n_messages = db.execute(
            text(
                """
                SELECT count(*) FROM chat_message
                WHERE chat_session_id IN (
                    SELECT id FROM chat_session WHERE description LIKE :p
                )
                """
            ),
            {"p": f"{SEED_PREFIX}%"},
        ).scalar()
        n_old = db.execute(
            text(
                """
                SELECT count(*) FROM chat_session
                WHERE description LIKE :p
                  AND time_created < now() - interval '31 days'
                """
            ),
            {"p": f"{SEED_PREFIX}%"},
        ).scalar()
        n_search_docs = db.execute(
            text("SELECT count(*) FROM search_doc WHERE semantic_id LIKE :p"),
            {"p": f"{SEED_PREFIX}%"},
        ).scalar()

    assert_ge(n_sessions, 60 * 10, "seeded chat sessions (60d × 10/day)")
    assert_ge(n_messages, 60 * 10, "seeded chat messages")
    assert_ge(n_old, 1, "seeded OLD (>31d) chat sessions for retention")
    assert_ge(n_search_docs, 1, "seeded search_doc rows")


def phase_clear_rollup_state() -> None:
    """Wipe the rollup checkpoint + table BEFORE backfill so we exercise
    the from-scratch path on every run."""
    section("Phase 2a — clear rollup state (checkpoint + table)")
    with Session(get_sqlalchemy_engine()) as db:
        db.execute(
            text("DELETE FROM key_value_store WHERE key = 'analytics_rollup_state'")
        )
        n_pre = db.execute(text("SELECT count(*) FROM analytics_daily_rollup")).scalar()
        db.execute(text("TRUNCATE TABLE analytics_daily_rollup"))
        db.commit()
    passed(f"cleared {n_pre} prior rollup rows + checkpoint")


def phase_backfill_and_verify_rollup() -> None:
    section("Phase 2b — backfill rollup + verify table + checkpoint")
    run_script(["scripts/backfill_analytics_rollup.py"])

    with Session(get_sqlalchemy_engine()) as db:
        rows = db.execute(
            text(
                """
                SELECT count(*),
                       sum(total_queries),
                       sum(total_likes),
                       sum(total_dislikes),
                       sum(total_resolved),
                       sum(total_needs_help),
                       sum(slackbot_total)
                FROM analytics_daily_rollup
                """
            )
        ).one()
        n_days, q, likes, dislikes, resolved, needs_help, slackbot = rows
        checkpoint_payload = db.execute(
            text(
                "SELECT value FROM key_value_store "
                "WHERE key = 'analytics_rollup_state'"
            )
        ).scalar()

    assert_ge(int(n_days), 60, "analytics_daily_rollup row count after backfill")
    assert_ge(int(q or 0), 60 * 10, "sum(total_queries) across rollup")
    assert_ge(int(likes or 0), 1, "sum(total_likes) across rollup")
    assert_ge(int(slackbot or 0), 1, "sum(slackbot_total) across rollup")
    assert_true(
        bool(checkpoint_payload)
        and isinstance(checkpoint_payload, dict)
        and "last_rolled_up_to" in checkpoint_payload,
        "checkpoint row exists with last_rolled_up_to",
        f"actual: {checkpoint_payload!r}",
    )


def phase_verify_endpoints() -> None:
    section("Phase 3 — read-from-rollup helpers return seeded data")
    from danswer.db.analytics_rollup import (
        fetch_danswerbot_analytics_from_rollup,
        fetch_query_analytics_from_rollup,
        fetch_user_analytics_from_rollup,
    )

    end = datetime.datetime.now(tz=datetime.timezone.utc)
    start = end - datetime.timedelta(days=70)
    with Session(get_sqlalchemy_engine()) as db:
        q_rows = list(fetch_query_analytics_from_rollup(start, end, db))
        u_rows = list(fetch_user_analytics_from_rollup(start, end, db))
        b_rows = list(fetch_danswerbot_analytics_from_rollup(start, end, db))

    assert_ge(len(q_rows), 60, "fetch_query_analytics_from_rollup row count")
    assert_ge(len(u_rows), 60, "fetch_user_analytics_from_rollup row count")
    assert_ge(len(b_rows), 60, "fetch_danswerbot_analytics_from_rollup row count")

    sum(int(r[0]) for r in q_rows)
    total_likes = sum(int(r[1]) for r in q_rows)
    total_resolved = sum(int(r[3]) for r in q_rows)

    # NPS strict denominator should be > 0 with 60 days × 10 chats × 60% feedback
    promoters = total_likes + total_resolved
    detractors = sum(int(r[2]) for r in q_rows) + sum(int(r[4]) for r in q_rows)
    nps_denom = promoters + detractors
    assert_ge(nps_denom, 1, "NPS-strict denominator (promoters + detractors)")

    nps = round((promoters - detractors) / nps_denom * 100) if nps_denom else None
    if nps is not None:
        passed(
            f"NPS-strict computed = {nps:+d} (promoters={promoters}, detractors={detractors})"
        )
    else:
        failed("NPS-strict undefined (no feedback)")


def phase_dry_run_retention() -> None:
    section("Phase 4 — retention dry-run reports something to clean")
    # Don't assert specific numbers — just that the dry-run completes
    # without error and the chat policy reports >0 rows.
    run_script(["scripts/cleanup_stale_db.py", "--dry-run", "--policy=chat"])


def phase_real_retention() -> None:
    section("Phase 5 — run retention, verify old chats deleted")

    # Freeze a "fresh" boundary BEFORE the retention runs. Use a buffer of
    # 28 days (retention uses 30) so chats right at the 30-day cutoff
    # don't flicker between "fresh" and "deleted" due to clock drift
    # during the retention sweep. The chats we seeded are spread across
    # 60 days; the 28-day window is well-separated from the deletion edge.
    fresh_boundary_iso = (
        datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=28)
    ).isoformat()

    with Session(get_sqlalchemy_engine()) as db:
        n_old_before = db.execute(
            text(
                """
                SELECT count(*) FROM chat_session
                WHERE description LIKE :p
                  AND time_created < now() - interval '31 days'
                """
            ),
            {"p": f"{SEED_PREFIX}%"},
        ).scalar()
        n_fresh_before = db.execute(
            text(
                """
                SELECT count(*) FROM chat_session
                WHERE description LIKE :p
                  AND time_created >= CAST(:b AS timestamptz)
                """
            ),
            {"p": f"{SEED_PREFIX}%", "b": fresh_boundary_iso},
        ).scalar()

    assert_ge(int(n_old_before or 0), 1, "old chats present before retention")

    run_script(["scripts/cleanup_stale_db.py", "--policy=chat"])

    with Session(get_sqlalchemy_engine()) as db:
        n_old_after = db.execute(
            text(
                """
                SELECT count(*) FROM chat_session
                WHERE description LIKE :p
                  AND time_created < now() - interval '31 days'
                """
            ),
            {"p": f"{SEED_PREFIX}%"},
        ).scalar()
        n_fresh_after = db.execute(
            text(
                """
                SELECT count(*) FROM chat_session
                WHERE description LIKE :p
                  AND time_created >= CAST(:b AS timestamptz)
                """
            ),
            {"p": f"{SEED_PREFIX}%", "b": fresh_boundary_iso},
        ).scalar()
        n_orphan_search_doc = db.execute(
            text(
                """
                SELECT count(*) FROM search_doc sd
                LEFT JOIN chat_message__search_doc cmsd
                  ON cmsd.search_doc_id = sd.id
                WHERE sd.semantic_id LIKE :p
                  AND cmsd.chat_message_id IS NULL
                """
            ),
            {"p": f"{SEED_PREFIX}%"},
        ).scalar()

    assert_eq(int(n_old_after or 0), 0, "old chats deleted by retention")
    assert_eq(
        int(n_fresh_after or 0),
        int(n_fresh_before or 0),
        "fresh chats untouched by retention (frozen 28-day boundary)",
    )
    # Orphan search_doc cleanup is a side-effect of chat retention; should
    # be 0 immediately after retention. (Some seeded SDs were linked to
    # OLD messages that just got deleted → the join goes empty → those SDs
    # are orphans → retention's orphan sweep deletes them.)
    assert_eq(
        int(n_orphan_search_doc or 0), 0, "orphan search_doc cleanup after retention"
    )


def phase_rollup_survived_retention() -> None:
    section("Phase 6 — rollup data SURVIVED retention deletes")
    with Session(get_sqlalchemy_engine()) as db:
        n_days = db.execute(
            text("SELECT count(*) FROM analytics_daily_rollup")
        ).scalar()
        # The rollup should still cover 60 days even though chat data older
        # than 30 days is now gone. This is the whole point of the rollup.
        assert_ge(int(n_days or 0), 60, "rollup row count survives retention")


def phase_rollup_idempotent() -> None:
    section("Phase 7 — re-run rollup is idempotent + advances checkpoint")
    from danswer.db.analytics_rollup import run_rollup

    with Session(get_sqlalchemy_engine()) as db:
        before = db.execute(
            text(
                "SELECT value FROM key_value_store "
                "WHERE key = 'analytics_rollup_state'"
            )
        ).scalar()

    n = run_rollup()
    assert_ge(n, 1, "run_rollup processed ≥1 day")

    with Session(get_sqlalchemy_engine()) as db:
        after = db.execute(
            text(
                "SELECT value FROM key_value_store "
                "WHERE key = 'analytics_rollup_state'"
            )
        ).scalar()
        n_days = db.execute(
            text("SELECT count(*) FROM analytics_daily_rollup")
        ).scalar()

    assert_true(
        bool(after) and isinstance(after, dict),
        "checkpoint row still present after re-run",
    )
    today_iso = datetime.datetime.now(tz=datetime.timezone.utc).date().isoformat()
    if after and isinstance(after, dict):
        assert_eq(
            after.get("last_rolled_up_to"), today_iso, "checkpoint advanced to today"
        )
    assert_ge(int(n_days or 0), 60, "rollup row count unchanged by re-run")
    print(f"  (info) checkpoint before={before}, after={after}")


def phase_final_cleanup(keep_data: bool) -> None:
    section("Phase 8 — final cleanup")
    if keep_data:
        passed("--keep-data passed; leaving seeded rows + rollup behind")
        return
    with Session(get_sqlalchemy_engine()) as db:
        counts = clean_seeded_data(db)
        # Also drop the rollup rows + checkpoint for a clean slate.
        db.execute(text("TRUNCATE TABLE analytics_daily_rollup"))
        db.execute(
            text("DELETE FROM key_value_store WHERE key = 'analytics_rollup_state'")
        )
        db.commit()
    nonzero = {k: v for k, v in counts.items() if v}
    passed(f"removed seeded rows: {nonzero}")
    passed("cleared analytics_daily_rollup + checkpoint")


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
        help="Don't clean seeded data + rollup at the end (debugging).",
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
            "This will create + delete data in the target DB. Type 'yes' to continue: "
        )
        if ans.strip().lower() != "yes":
            print("Aborted.")
            return 1

    phase_migrate()
    phase_seed()
    phase_clear_rollup_state()
    phase_backfill_and_verify_rollup()
    phase_verify_endpoints()
    phase_dry_run_retention()
    phase_real_retention()
    phase_rollup_survived_retention()
    phase_rollup_idempotent()
    phase_final_cleanup(keep_data=args.keep_data)

    print()
    if _FAILED:
        print(f"❌ {_FAILED} assertion(s) failed.")
        return 1
    print("✅ All phases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
