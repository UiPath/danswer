"""End-to-end smoke tests for non-analytics features built this session.

Companion to `test_analytics_e2e.py`. That script covers the analytics +
chat retention pipelines; this one covers everything else:

  Phase 1  Per-attempt indexing priority — verifies
           `get_not_started_index_attempts` ordering (priority DESC,
           time_created ASC) and `update_index_attempt_priority`
           semantics (only NOT_STARTED is mutable).
  Phase 2  index_attempt retention — opt-in via
           RETENTION_DAYS_INDEX_ATTEMPT, with keep-last-N. Critically
           also verifies the P0 status-casing fix: the SQL filters use
           lowercase 'success' / 'failed' to match how
           `Enum(IndexingStatus, native_enum=False)` actually stores
           them. An uppercase regression here silently no-ops the policy.
  Phase 3  permission_sync_run retention — only TERMINAL rows
           ('success' / 'failed') are deleted; 'in_progress' rows are
           preserved regardless of age, matching the safety contract in
           db/retention.py.
  Phase 4  Resolved-button feedback DB write — synthetic call to
           `create_chat_message_feedback(predefined_feedback='resolved',
            ...)` against a seeded chat message. Verifies the row shape
           the Slackbot resolved-button handler relies on.

Each phase prints PASS/FAIL with context. Exits 0 on full success,
non-zero on the first failed assertion. Re-run safe — idempotent.

DESTRUCTIVE: writes/deletes tagged data only (`__test_features__` prefix).
Run only against a dev / staging DB.

Usage:
    cd backend
    PYTHONPATH=$(pwd) python scripts/test_features_e2e.py [--yes] [--keep-data]
"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.orm import Session

from danswer.auth.schemas import UserRole
from danswer.configs.constants import DocumentSource
from danswer.configs.constants import MessageType
from danswer.connectors.models import InputType
from danswer.db.engine import get_sqlalchemy_engine
from danswer.db.feedback import create_chat_message_feedback
from danswer.db.index_attempt import get_not_started_index_attempts
from danswer.db.index_attempt import mark_attempt_in_progress__no_commit
from danswer.db.index_attempt import update_index_attempt_priority
from danswer.db.models import (
    ChatMessage,
    ChatMessageFeedback,
    ChatSession,
    ChatSessionSharedStatus,
    Connector,
    ConnectorCredentialPair,
    Credential,
    EmbeddingModel,
    IndexAttempt,
    IndexingStatus,
    Persona,
    User,
)


FEATURE_PREFIX = "__test_features__"


# ---------------------------------------------------------------------------
# Tiny harness (mirrors test_analytics_e2e.py for consistency)
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


def assert_list_eq(actual: list, expected: list, label: str) -> None:
    if list(actual) == list(expected):
        passed(f"{label} — {actual}")
    else:
        failed(label, f"expected={expected}, actual={list(actual)}")


def assert_true(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        passed(label)
    else:
        failed(label, detail or None)


# ---------------------------------------------------------------------------
# Shared fixture helpers (all tagged with FEATURE_PREFIX)
# ---------------------------------------------------------------------------


def lookup_persona(db: Session) -> Persona:
    p = db.execute(select(Persona).order_by(Persona.id).limit(1)).scalar_one_or_none()
    if p is None:
        sys.exit(
            "No persona found. Bootstrap your DB first (alembic upgrade head + "
            "start the API server once)."
        )
    return p


def lookup_embedding_model(db: Session) -> EmbeddingModel:
    m = db.execute(
        select(EmbeddingModel).order_by(EmbeddingModel.id).limit(1)
    ).scalar_one_or_none()
    if m is None:
        sys.exit("No embedding_model found.")
    return m


def make_user(db: Session) -> User:
    u = User(
        id=uuid.uuid4(),
        email=f"{FEATURE_PREFIX}user-{uuid.uuid4().hex[:6]}@example.test",
        hashed_password="x" * 60,
        is_active=True,
        is_superuser=False,
        is_verified=True,
        role=UserRole.BASIC,
    )
    db.add(u)
    db.flush()
    return u


def make_connector_and_pair(
    db: Session, source: DocumentSource = DocumentSource.GITHUB
) -> tuple[Connector, Credential, ConnectorCredentialPair]:
    connector = Connector(
        name=f"{FEATURE_PREFIX}connector-{source.value}-{uuid.uuid4().hex[:6]}",
        source=source,
        input_type=InputType.POLL,
        connector_specific_config={"_test_features": True},
        refresh_freq=600,
        disabled=False,
    )
    credential = Credential(admin_public=True, credential_json={})
    db.add_all([connector, credential])
    db.flush()
    ccp = ConnectorCredentialPair(
        connector_id=connector.id,
        credential_id=credential.id,
        name=f"{FEATURE_PREFIX}ccp-{uuid.uuid4().hex[:6]}",
        is_public=True,
        total_docs_indexed=0,
    )
    db.add(ccp)
    db.flush()
    return connector, credential, ccp


def make_attempt(
    db: Session,
    *,
    connector_id: int,
    credential_id: int,
    embedding_model_id: int,
    status: IndexingStatus,
    priority: int = 0,
    days_old: int = 0,
) -> IndexAttempt:
    when = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(
        days=days_old, seconds=uuid.uuid4().int % 100
    )
    a = IndexAttempt(
        connector_id=connector_id,
        credential_id=credential_id,
        embedding_model_id=embedding_model_id,
        from_beginning=False,
        status=status,
        new_docs_indexed=0,
        total_docs_indexed=0,
        docs_removed_from_index=0,
        indexing_priority=priority,
        time_created=when,
        time_updated=when,
        time_started=when if status != IndexingStatus.NOT_STARTED else None,
    )
    db.add(a)
    db.flush()
    return a


# ---------------------------------------------------------------------------
# Phase 1 — Per-attempt indexing priority
# ---------------------------------------------------------------------------


def phase_priority_ordering() -> None:
    section("Phase 1 — per-attempt priority ordering + update semantics")

    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        persona = lookup_persona(db)
        em = lookup_embedding_model(db)
        _u = make_user(db)
        _conn, _cred, ccp = make_connector_and_pair(db)

        # Insert 5 NOT_STARTED with priorities, in this creation order:
        #   a (pri=0,  oldest)
        #   b (pri=5)
        #   c (pri=10)
        #   d (pri=0)
        #   e (pri=3,  newest)
        # Expected `get_not_started_index_attempts` order:
        #   c (10) > b (5) > e (3) > a (0, oldest of the 0s) > d (0)
        priorities = [0, 5, 10, 0, 3]
        attempts: list[IndexAttempt] = []
        for i, pri in enumerate(priorities):
            a = make_attempt(
                db,
                connector_id=ccp.connector_id,
                credential_id=ccp.credential_id,
                embedding_model_id=em.id,
                status=IndexingStatus.NOT_STARTED,
                priority=pri,
                days_old=0,
            )
            # Force monotonic time_created so the FIFO tiebreak is
            # deterministic regardless of microsecond clock drift.
            a.time_created = datetime.datetime.now(
                tz=datetime.timezone.utc
            ) - datetime.timedelta(seconds=(len(priorities) - i) * 10)
            attempts.append(a)
        db.commit()

        a, b, c, d, e = attempts
        all_not_started = get_not_started_index_attempts(db)
        # Filter to only ones we created (DB may have other NOT_STARTED rows
        # from other tests / production).
        ours = [x for x in all_not_started if x.id in {a.id, b.id, c.id, d.id, e.id}]
        ordered_priorities = [x.indexing_priority for x in ours]
        assert_list_eq(
            ordered_priorities,
            [10, 5, 3, 0, 0],
            "get_not_started_index_attempts: priority DESC ordering",
        )

        # Within the priority=0 tier, the OLDER one (a, time_created
        # earliest) should come before d (time_created later).
        zero_tier = [x.id for x in ours if x.indexing_priority == 0]
        assert_list_eq(
            zero_tier,
            [a.id, d.id],
            "get_not_started_index_attempts: time_created ASC tiebreak",
        )

        # update_index_attempt_priority on a NOT_STARTED row succeeds
        bumped = update_index_attempt_priority(a.id, 50, db)
        assert_true(
            bumped is not None and bumped.indexing_priority == 50,
            "update_index_attempt_priority: NOT_STARTED → priority bumped",
            f"got: {bumped.indexing_priority if bumped else None}",
        )

        # Bumping above the 100 ceiling is clamped
        capped = update_index_attempt_priority(a.id, 1000, db)
        assert_true(
            capped is not None and capped.indexing_priority == 100,
            "update_index_attempt_priority: clamps to ceiling=100",
            f"got: {capped.indexing_priority if capped else None}",
        )

        # Mark one as IN_PROGRESS — update should now refuse (returns None)
        mark_attempt_in_progress__no_commit(c)
        db.commit()
        rejected = update_index_attempt_priority(c.id, 99, db)
        assert_eq(
            rejected,
            None,
            "update_index_attempt_priority: IN_PROGRESS → refused (returns None)",
        )

        # Verify priority unchanged on c
        db.refresh(c)
        assert_eq(
            c.indexing_priority,
            10,
            "update_index_attempt_priority: IN_PROGRESS row priority untouched",
        )


# ---------------------------------------------------------------------------
# Phase 2 — index_attempt retention (also verifies P0 lowercase fix)
# ---------------------------------------------------------------------------


def phase_index_attempt_retention() -> None:
    section("Phase 2 — index_attempt retention (lowercase fix + keep_last_N)")

    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        persona = lookup_persona(db)
        em = lookup_embedding_model(db)
        _u = make_user(db)
        _conn, _cred, ccp = make_connector_and_pair(db)

        # Seed 25 SUCCESS attempts for one cc-pair, all 70 days old.
        # With RETENTION_DAYS_INDEX_ATTEMPT=60 and KEEP_LAST_N=20, the
        # retention sweep should delete the 5 oldest (rn > 20 partition rule).
        for i in range(25):
            make_attempt(
                db,
                connector_id=ccp.connector_id,
                credential_id=ccp.credential_id,
                embedding_model_id=em.id,
                status=IndexingStatus.SUCCESS,
                days_old=70 + i,  # day-spread keeps row_number deterministic
            )
        db.commit()
        before = (
            db.query(IndexAttempt)
            .filter(IndexAttempt.connector_id == ccp.connector_id)
            .filter(IndexAttempt.credential_id == ccp.credential_id)
            .count()
        )
        assert_eq(before, 25, "index_attempt rows seeded (25 SUCCESS, 70d+ old)")

    # Run retention with index_attempt enabled via env-var injection.
    # cleanup_stale_db.py reads RETENTION_DAYS_* at module import time, so
    # we have to spawn a subprocess with the env baked in.
    print("  $ RETENTION_DAYS_INDEX_ATTEMPT=60 RETENTION_KEEP_LAST_N_INDEX_ATTEMPTS=20 \\")
    print("    cleanup_stale_db.py --policy=index_attempt")
    env = {
        **os.environ,
        "RETENTION_DAYS_INDEX_ATTEMPT": "60",
        "RETENTION_KEEP_LAST_N_INDEX_ATTEMPTS": "20",
    }
    result = subprocess.run(
        [sys.executable, "scripts/cleanup_stale_db.py", "--policy=index_attempt"],
        cwd=str(THIS_DIR.parent),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        for line in (result.stdout or "").splitlines()[-10:]:
            print(f"    {line}")
        for line in (result.stderr or "").splitlines()[-10:]:
            print(f"    {line}")
        failed("cleanup_stale_db.py --policy=index_attempt failed")
        return
    # Print the policy summary table from cleanup_stale_db.py for context.
    for line in (result.stdout or "").splitlines():
        s = line.strip()
        if s.startswith("index_attempt") or s.startswith("policy"):
            print(f"    {line}")

    with Session(engine) as db:
        after = (
            db.query(IndexAttempt)
            .filter(IndexAttempt.connector_id == ccp.connector_id)
            .filter(IndexAttempt.credential_id == ccp.credential_id)
            .count()
        )
        # P0 fix verification: if SQL still used uppercase 'SUCCESS', this
        # would no-op and `after == 25`. Lowercase 'success' is what's
        # actually stored, so 5 should be deleted.
        assert_eq(
            after,
            20,
            "P0 lowercase status fix: 5 oldest deleted, 20 newest kept",
        )


# ---------------------------------------------------------------------------
# Phase 3 — permission_sync_run retention preserves in_progress
# ---------------------------------------------------------------------------


def phase_permission_sync_retention() -> None:
    section("Phase 3 — permission_sync_run retention preserves in_progress")

    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        _conn, _cred, ccp = make_connector_and_pair(db)
        # Insert via raw SQL because PermissionSyncRun.update_type / .status
        # are declared without native_enum=False; SA bulk insert tries to
        # cast to a non-existent PG enum type. (Same workaround as the
        # main seeder.)
        old = (
            datetime.datetime.now(tz=datetime.timezone.utc)
            - datetime.timedelta(days=90)
        )
        rows = [
            ("github", "user_level", ccp.id, "success", old),
            ("github", "user_level", ccp.id, "success", old),
            ("github", "user_level", ccp.id, "failed", old),
            ("github", "user_level", ccp.id, "failed", old),
            ("github", "user_level", ccp.id, "failed", old),
            ("github", "user_level", ccp.id, "in_progress", old),
            ("github", "user_level", ccp.id, "in_progress", old),
            ("github", "user_level", ccp.id, "in_progress", old),
        ]
        for src, ut, ccp_id, st, when in rows:
            db.execute(
                text(
                    """
                    INSERT INTO permission_sync_run
                      (source_type, update_type, cc_pair_id, status,
                       error_msg, updated_at)
                    VALUES
                      (:src, :ut, :ccp_id, :st, NULL, :when)
                    """
                ),
                {"src": src, "ut": ut, "ccp_id": ccp_id, "st": st, "when": when},
            )
        db.commit()

        before_total = db.execute(
            text(
                "SELECT count(*) FROM permission_sync_run WHERE cc_pair_id = :id"
            ),
            {"id": ccp.id},
        ).scalar()
        before_in_progress = db.execute(
            text(
                "SELECT count(*) FROM permission_sync_run "
                "WHERE cc_pair_id = :id AND status = 'in_progress'"
            ),
            {"id": ccp.id},
        ).scalar()
        assert_eq(int(before_total or 0), 8, "seeded permission_sync_run rows")
        assert_eq(
            int(before_in_progress or 0), 3, "seeded in_progress rows"
        )

    print("  $ cleanup_stale_db.py --policy=permission_sync_run")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/cleanup_stale_db.py",
            "--policy=permission_sync_run",
        ],
        cwd=str(THIS_DIR.parent),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        for line in (result.stderr or "").splitlines()[-10:]:
            print(f"    {line}")
        failed("cleanup_stale_db.py --policy=permission_sync_run failed")
        return

    with Session(engine) as db:
        after_total = db.execute(
            text(
                "SELECT count(*) FROM permission_sync_run WHERE cc_pair_id = :id"
            ),
            {"id": ccp.id},
        ).scalar()
        after_in_progress = db.execute(
            text(
                "SELECT count(*) FROM permission_sync_run "
                "WHERE cc_pair_id = :id AND status = 'in_progress'"
            ),
            {"id": ccp.id},
        ).scalar()
        after_terminal = db.execute(
            text(
                "SELECT count(*) FROM permission_sync_run "
                "WHERE cc_pair_id = :id AND status IN ('success', 'failed')"
            ),
            {"id": ccp.id},
        ).scalar()

    assert_eq(int(after_terminal or 0), 0, "terminal rows deleted (success + failed)")
    assert_eq(
        int(after_in_progress or 0),
        3,
        "in_progress rows preserved despite 90d age",
    )
    assert_eq(int(after_total or 0), 3, "only in_progress remain")


# ---------------------------------------------------------------------------
# Phase 4 — Resolved-button feedback DB write path
# ---------------------------------------------------------------------------


def phase_resolved_feedback_write() -> None:
    section("Phase 4 — resolved-feedback DB write path")

    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        persona = lookup_persona(db)
        # Slackbot sessions have user_id=NULL by design (the slackbot
        # handler has no Danswer user, just a Slack user). The
        # `create_chat_message_feedback` permission check rejects
        # ownership mismatches, so the test session must mirror that.
        when = datetime.datetime.now(tz=datetime.timezone.utc)
        cs = ChatSession(
            user_id=None,
            persona_id=persona.id,
            description=f"{FEATURE_PREFIX}resolved-feedback-{uuid.uuid4().hex[:6]}",
            deleted=False,
            one_shot=False,
            shared_status=ChatSessionSharedStatus.PRIVATE,
            danswerbot_flow=True,
            time_created=when,
            time_updated=when,
        )
        db.add(cs)
        db.flush()
        msg = ChatMessage(
            chat_session_id=cs.id,
            message="Test reply",
            message_type=MessageType.ASSISTANT,
            token_count=5,
            time_sent=when,
        )
        db.add(msg)
        db.flush()
        msg_id = msg.id
        db.commit()

        # Same call shape as `handle_followup_resolved_button` (post-fix):
        # chat_message_id, predefined_feedback='resolved', no is_positive.
        create_chat_message_feedback(
            is_positive=None,
            feedback_text="",
            chat_message_id=msg_id,
            user_id=None,
            db_session=db,
            predefined_feedback="resolved",
        )

        rows = (
            db.query(ChatMessageFeedback)
            .filter(ChatMessageFeedback.chat_message_id == msg_id)
            .all()
        )

    assert_eq(len(rows), 1, "exactly one feedback row written")
    if not rows:
        return
    fb = rows[0]
    assert_eq(fb.predefined_feedback, "resolved", "predefined_feedback='resolved'")
    assert_eq(fb.is_positive, None, "is_positive is NULL for resolved")
    assert_eq(fb.required_followup, None, "required_followup is NULL")
    assert_eq(fb.chat_message_id, msg_id, "chat_message_id matches")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_test_data() -> None:
    """Drop everything tagged with FEATURE_PREFIX, FK-safe order."""
    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        # chat_feedback for our messages (auto SET NULL would zombie them)
        db.execute(
            text(
                """
                DELETE FROM chat_feedback
                WHERE chat_message_id IN (
                    SELECT cm.id FROM chat_message cm
                    JOIN chat_session cs ON cs.id = cm.chat_session_id
                    WHERE cs.description LIKE :p
                )
                """
            ),
            {"p": f"{FEATURE_PREFIX}%"},
        )
        # chat_message__search_doc / tool_call / chat_message
        db.execute(
            text(
                """
                DELETE FROM chat_message__search_doc
                WHERE chat_message_id IN (
                    SELECT cm.id FROM chat_message cm
                    JOIN chat_session cs ON cs.id = cm.chat_session_id
                    WHERE cs.description LIKE :p
                )
                """
            ),
            {"p": f"{FEATURE_PREFIX}%"},
        )
        db.execute(
            text(
                """
                DELETE FROM tool_call WHERE message_id IN (
                    SELECT cm.id FROM chat_message cm
                    JOIN chat_session cs ON cs.id = cm.chat_session_id
                    WHERE cs.description LIKE :p
                )
                """
            ),
            {"p": f"{FEATURE_PREFIX}%"},
        )
        db.execute(
            text(
                """
                DELETE FROM chat_message
                WHERE chat_session_id IN (
                    SELECT id FROM chat_session WHERE description LIKE :p
                )
                """
            ),
            {"p": f"{FEATURE_PREFIX}%"},
        )
        db.execute(
            text("DELETE FROM chat_session WHERE description LIKE :p"),
            {"p": f"{FEATURE_PREFIX}%"},
        )
        # permission_sync_run + index_attempt linked to seeded ccps
        db.execute(
            text(
                """
                DELETE FROM permission_sync_run
                WHERE cc_pair_id IN (
                    SELECT id FROM connector_credential_pair WHERE name LIKE :p
                )
                """
            ),
            {"p": f"{FEATURE_PREFIX}%"},
        )
        db.execute(
            text(
                """
                DELETE FROM index_attempt
                WHERE connector_id IN (
                    SELECT id FROM connector WHERE name LIKE :p
                )
                """
            ),
            {"p": f"{FEATURE_PREFIX}%"},
        )
        # cc_pair → connector
        db.execute(
            text("DELETE FROM connector_credential_pair WHERE name LIKE :p"),
            {"p": f"{FEATURE_PREFIX}%"},
        )
        db.execute(
            text("DELETE FROM connector WHERE name LIKE :p"),
            {"p": f"{FEATURE_PREFIX}%"},
        )
        # users last
        db.execute(
            text("""DELETE FROM "user" WHERE email LIKE :p"""),
            {"p": f"{FEATURE_PREFIX}%"},
        )
        db.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


THIS_DIR = Path(__file__).resolve().parent


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
            "This will create + delete tagged data in the target DB. "
            "Type 'yes' to continue: "
        )
        if ans.strip().lower() != "yes":
            print("Aborted.")
            return 1

    # Best-effort pre-clean — if a prior run left tagged junk behind, clear it.
    cleanup_test_data()

    try:
        phase_priority_ordering()
        phase_index_attempt_retention()
        phase_permission_sync_retention()
        phase_resolved_feedback_write()
    finally:
        if not args.keep_data:
            cleanup_test_data()
            print("\n  cleaned up tagged test data")

    print()
    if _FAILED:
        print(f"❌ {_FAILED} assertion(s) failed.")
        return 1
    print("✅ All phases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
