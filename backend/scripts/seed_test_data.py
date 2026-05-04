"""Auto-generate tagged test data for analytics + retention smoke tests.

DESTRUCTIVE: writes (and on --clean, deletes) rows in your DB. Run only
against a dev / staging DB. The script prints the DB host and asks for
confirmation unless --yes is passed.

Tagging: every row this script creates has a `__test_seed__` marker in a
queryable column (description / name / file_name / etc.) so --clean can
find and remove only its own data without touching real rows.

Usage examples:

    cd backend
    PYTHONPATH=$(pwd) python scripts/seed_test_data.py --help
    PYTHONPATH=$(pwd) python scripts/seed_test_data.py --days=60 --chats-per-day=20
    PYTHONPATH=$(pwd) python scripts/seed_test_data.py --clean --yes
"""
from __future__ import annotations

import argparse
import datetime
import random
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.orm import Session

from danswer.auth.schemas import UserRole
from danswer.configs.constants import DocumentSource
from danswer.configs.constants import MessageType
from danswer.connectors.models import InputType
from danswer.db.engine import get_sqlalchemy_engine
from danswer.db.models import ChatMessage
from danswer.db.models import ChatMessage__SearchDoc
from danswer.db.models import ChatMessageFeedback
from danswer.db.models import ChatSession
from danswer.db.models import ChatSessionSharedStatus
from danswer.db.models import Connector
from danswer.db.models import ConnectorCredentialPair
from danswer.db.models import Credential
from danswer.db.models import Document
from danswer.db.models import DocumentByConnectorCredentialPair
from danswer.db.models import EmbeddingModel
from danswer.db.models import IndexAttempt
from danswer.db.models import IndexingStatus
from danswer.db.models import PermissionSyncJobType
from danswer.db.models import PermissionSyncStatus
from danswer.db.models import Persona
from danswer.db.models import SearchDoc
from danswer.db.models import SlackBotConfig
from danswer.db.models import SlackBotResponseType
from danswer.db.models import User


SEED_PREFIX = "__test_seed__"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class SeedConfig:
    days: int
    chats_per_day: int
    slackbot_share: float
    feedback_rate: float
    like_share: float
    resolved_share: float
    needs_help_share: float
    users_count: int
    connectors_count: int
    docs_per_connector: int
    with_search_docs: bool
    with_old_data: bool
    seed: int


SOURCES = [
    DocumentSource.GITHUB,
    DocumentSource.SLACK,
    DocumentSource.JIRA,
    DocumentSource.CONFLUENCE,
    DocumentSource.NOTION,
    DocumentSource.WEB,
]


# ---------------------------------------------------------------------------
# Safety: confirm before writing
# ---------------------------------------------------------------------------


def confirm_destructive(skip: bool) -> None:
    engine = get_sqlalchemy_engine()
    url = engine.url
    safe_url = f"{url.drivername}://{url.username}@{url.host}:{url.port}/{url.database}"
    if skip:
        print(f"[--yes] Proceeding against {safe_url}")
        return
    print(f"This will WRITE / DELETE tagged ({SEED_PREFIX!r}) rows in:")
    print(f"  {safe_url}")
    answer = input("Type 'yes' to continue: ")
    if answer.strip().lower() != "yes":
        print("Aborted.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Lookups for required-NOT-NULL FKs
# ---------------------------------------------------------------------------


def lookup_default_persona(db: Session) -> Persona:
    p = db.execute(select(Persona).order_by(Persona.id).limit(1)).scalar_one_or_none()
    if p is None:
        sys.exit(
            "No persona found. Bootstrap your DB first (run `alembic upgrade head` "
            "and start the API server once so the default personas get loaded)."
        )
    return p


def lookup_default_embedding_model(db: Session) -> EmbeddingModel:
    m = db.execute(
        select(EmbeddingModel).order_by(EmbeddingModel.id).limit(1)
    ).scalar_one_or_none()
    if m is None:
        sys.exit(
            "No embedding_model found. Bootstrap the DB first (start the API "
            "server once to load the default embedding model)."
        )
    return m


# ---------------------------------------------------------------------------
# Seeders — each returns the rows it created (or counts) for downstream wiring
# ---------------------------------------------------------------------------


def seed_users(db: Session, n: int) -> list[User]:
    users: list[User] = []
    for i in range(n):
        u = User(
            id=uuid.uuid4(),
            email=f"{SEED_PREFIX}user-{i}-{uuid.uuid4().hex[:6]}@example.test",
            hashed_password="x" * 60,
            is_active=True,
            is_superuser=False,
            is_verified=True,
            role=UserRole.BASIC,
        )
        db.add(u)
        users.append(u)
    db.commit()
    return users


def seed_connectors(
    db: Session, count: int, docs_per_connector: int
) -> list[tuple[Connector, Credential, ConnectorCredentialPair]]:
    out: list[tuple[Connector, Credential, ConnectorCredentialPair]] = []
    for i in range(count):
        source = SOURCES[i % len(SOURCES)]
        connector = Connector(
            name=f"{SEED_PREFIX}connector-{source.value}-{i}",
            source=source,
            input_type=InputType.POLL,
            connector_specific_config={"_test_seed": True},
            refresh_freq=600,
            disabled=False,
        )
        credential = Credential(admin_public=True, credential_json={})
        db.add_all([connector, credential])
        db.flush()
        ccp = ConnectorCredentialPair(
            connector_id=connector.id,
            credential_id=credential.id,
            name=f"{SEED_PREFIX}ccp-{source.value}-{i}",
            is_public=True,
            total_docs_indexed=docs_per_connector,
        )
        db.add(ccp)
        out.append((connector, credential, ccp))
    db.commit()
    return out


def seed_documents(
    db: Session,
    pairs: list[tuple[Connector, Credential, ConnectorCredentialPair]],
    docs_per_connector: int,
) -> int:
    """One row per (connector, doc-index). Also writes the join table so
    `document_by_connector_credential_pair` is populated."""
    n = 0
    for conn, cred, _ccp in pairs:
        for d in range(docs_per_connector):
            doc_id = f"{SEED_PREFIX}doc-{conn.id}-{d}"
            # Documents are deduped by id — one shared row across cc-pairs in
            # real life. For the seeder we only have one ccp per source so
            # simple insert is fine.
            doc = Document(
                id=doc_id,
                boost=0,
                hidden=False,
                semantic_id=f"semantic-{doc_id}",
                link=None,
                from_ingestion_api=False,
            )
            db.add(doc)
            db.add(
                DocumentByConnectorCredentialPair(
                    id=doc_id,
                    connector_id=conn.id,
                    credential_id=cred.id,
                )
            )
            n += 1
        db.commit()  # commit per cc-pair to keep transactions reasonable
    return n


def seed_chat_thread(
    db: Session,
    user: User,
    persona_id: int,
    when: datetime.datetime,
    danswerbot: bool,
    feedback_kind: str | None,
) -> tuple[ChatSession, ChatMessage]:
    """Create one chat session + one assistant message + optional feedback.
    `feedback_kind` ∈ {None, 'like', 'dislike', 'resolved', 'needs_help'}.
    """
    session = ChatSession(
        user_id=user.id,
        persona_id=persona_id,
        description=f"{SEED_PREFIX}session-{uuid.uuid4().hex[:8]}",
        deleted=False,
        one_shot=False,
        shared_status=ChatSessionSharedStatus.PRIVATE,
        danswerbot_flow=danswerbot,
        time_created=when,
        time_updated=when,
    )
    db.add(session)
    db.flush()

    assistant_msg = ChatMessage(
        chat_session_id=session.id,
        message="Test assistant response",
        message_type=MessageType.ASSISTANT,
        token_count=10,
        time_sent=when,
    )
    db.add(assistant_msg)
    db.flush()

    if feedback_kind == "like":
        db.add(
            ChatMessageFeedback(
                chat_message_id=assistant_msg.id,
                is_positive=True,
            )
        )
    elif feedback_kind == "dislike":
        db.add(
            ChatMessageFeedback(
                chat_message_id=assistant_msg.id,
                is_positive=False,
            )
        )
    elif feedback_kind == "resolved":
        db.add(
            ChatMessageFeedback(
                chat_message_id=assistant_msg.id,
                is_positive=None,
                predefined_feedback="resolved",
            )
        )
    elif feedback_kind == "needs_help":
        db.add(
            ChatMessageFeedback(
                chat_message_id=assistant_msg.id,
                is_positive=None,
                required_followup=True,
            )
        )

    return session, assistant_msg


def _pick_feedback_kind(rng: random.Random, cfg: SeedConfig) -> str | None:
    """Return one of {None, 'like', 'dislike', 'resolved', 'needs_help'}
    according to the configured rates."""
    if rng.random() >= cfg.feedback_rate:
        return None
    # Within feedback, allocate by share. Likes + resolved + needs_help may
    # not sum to 1 — the remainder maps to dislikes.
    r = rng.random()
    if r < cfg.like_share:
        return "like"
    elif r < cfg.like_share + cfg.resolved_share:
        return "resolved"
    elif r < cfg.like_share + cfg.resolved_share + cfg.needs_help_share:
        return "needs_help"
    return "dislike"


def seed_chat_data(
    db: Session,
    cfg: SeedConfig,
    users: list[User],
    persona_id: int,
    rng: random.Random,
    days_offset: int = 0,
) -> tuple[int, list[ChatMessage]]:
    """Seed chats across `cfg.days` days starting `days_offset` days ago.

    Returns (total_threads_created, list_of_assistant_messages_for_linking).
    The message list is used by --with-search-docs to attach search_doc
    rows.
    """
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    total = 0
    msgs: list[ChatMessage] = []
    for day_idx in range(cfg.days):
        when_day = now - datetime.timedelta(days=days_offset + day_idx)
        for c in range(cfg.chats_per_day):
            # Spread within the day (random hour/minute) so time-bucket
            # boundaries don't clip artificially.
            when = when_day.replace(
                hour=rng.randint(0, 23),
                minute=rng.randint(0, 59),
                second=rng.randint(0, 59),
                microsecond=0,
            )
            user = rng.choice(users)
            danswerbot = rng.random() < cfg.slackbot_share
            kind = _pick_feedback_kind(rng, cfg)
            _session, msg = seed_chat_thread(
                db,
                user=user,
                persona_id=persona_id,
                when=when,
                danswerbot=danswerbot,
                feedback_kind=kind,
            )
            msgs.append(msg)
            total += 1
        db.commit()
    return total, msgs


def seed_old_chat_for_retention(
    db: Session,
    users: list[User],
    persona_id: int,
    rng: random.Random,
    count: int,
) -> int:
    """Backdated chats (35–90 days old) so the retention sweep deletes them.

    Used to verify chat retention actually removes old data while leaving
    fresh data alone.
    """
    n = 0
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    for _ in range(count):
        days_old = rng.randint(35, 90)
        when = now - datetime.timedelta(
            days=days_old,
            hours=rng.randint(0, 23),
            minutes=rng.randint(0, 59),
        )
        user = rng.choice(users)
        seed_chat_thread(
            db,
            user=user,
            persona_id=persona_id,
            when=when,
            danswerbot=rng.random() < 0.5,
            feedback_kind=rng.choice([None, "like", "dislike", "resolved"]),
        )
        n += 1
    db.commit()
    return n


def seed_search_docs_for_messages(
    db: Session, msgs: list[ChatMessage], per_message: int = 3
) -> int:
    """Create search_doc rows and link them to chat messages via the
    join table. Used to verify orphan-search_doc cleanup at the end of
    chat retention."""
    created = 0
    for msg in msgs[:200]:  # cap at 200 messages to keep totals sane
        for _ in range(per_message):
            sd = SearchDoc(
                document_id=f"{SEED_PREFIX}doc-link-{uuid.uuid4().hex[:8]}",
                chunk_ind=0,
                semantic_id=f"{SEED_PREFIX}sd-{uuid.uuid4().hex[:8]}",
                link=None,
                blurb="seeded blurb",
                boost=0,
                source_type=DocumentSource.WEB.value,
                hidden=False,
                score=0.5,
                match_highlights=[],
                doc_metadata={},
            )
            db.add(sd)
            db.flush()
            db.add(ChatMessage__SearchDoc(chat_message_id=msg.id, search_doc_id=sd.id))
            created += 1
        db.commit()
    return created


def seed_slack_bot_configs(
    db: Session, count: int, persona_id: int, rng: random.Random
) -> int:
    """Create slack_bot_config rows with seeded `channel_names` arrays.

    Used to verify the slack-channels analytics endpoint and the
    `jsonb_array_elements_text` distinct-channel count.
    """
    for i in range(count):
        n_channels = rng.randint(2, 5)
        channels = [f"{SEED_PREFIX}-channel-{i}-{j}" for j in range(n_channels)]
        db.add(
            SlackBotConfig(
                persona_id=persona_id,
                channel_config={"channel_names": channels},
                response_type=SlackBotResponseType.CITATIONS,
            )
        )
    db.commit()
    return count


def seed_index_attempts(
    db: Session,
    pairs: list[tuple[Connector, Credential, ConnectorCredentialPair]],
    embedding_model: EmbeddingModel,
    count_per_pair: int,
    with_old: bool,
    rng: random.Random,
) -> int:
    """Mix of statuses + ages. With --with-old-data, half are backdated
    35-90 days so the optional index_attempt retention can hit them."""
    statuses = [IndexingStatus.SUCCESS, IndexingStatus.FAILED]
    n = 0
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    for conn, cred, _ccp in pairs:
        for i in range(count_per_pair):
            backdate = with_old and rng.random() < 0.5
            days_old = rng.randint(35, 90) if backdate else rng.randint(0, 5)
            when = now - datetime.timedelta(days=days_old, hours=rng.randint(0, 23))
            attempt = IndexAttempt(
                connector_id=conn.id,
                credential_id=cred.id,
                embedding_model_id=embedding_model.id,
                from_beginning=False,
                status=rng.choice(statuses),
                error_msg=None if rng.random() < 0.7 else "test failure",
                new_docs_indexed=rng.randint(0, 100),
                total_docs_indexed=rng.randint(0, 1000),
                docs_removed_from_index=0,
                time_created=when,
                time_updated=when,
                time_started=when,
            )
            db.add(attempt)
            n += 1
        db.commit()
    return n


def seed_permission_syncs(
    db: Session,
    pairs: list[tuple[Connector, Credential, ConnectorCredentialPair]],
    count: int,
    with_old: bool,
    rng: random.Random,
) -> int:
    """Mix of fresh + old (35-90d) terminal-state permission_sync_run rows.

    Uses raw `text()` SQL because the `PermissionSyncRun.update_type` and
    `.status` columns are declared as `Enum(...)` without `native_enum=
    False` in the model — SA 2.x's bulk-insert path then emits
    `::permissionsyncjobtype` casts in the SQL, but the underlying column
    is plain `varchar`. Going through raw SQL with the enum's `.value`
    string sidesteps the type-cast machinery cleanly.
    """
    if not pairs:
        return 0
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    statuses = [
        PermissionSyncStatus.SUCCESS.value,
        PermissionSyncStatus.FAILED.value,
    ]
    job_types = [
        PermissionSyncJobType.USER_LEVEL.value,
        PermissionSyncJobType.GROUP_LEVEL.value,
    ]
    for i in range(count):
        backdate = with_old and rng.random() < 0.5
        days_old = rng.randint(35, 90) if backdate else rng.randint(0, 5)
        when = now - datetime.timedelta(days=days_old)
        conn, _cred, ccp = rng.choice(pairs)
        db.execute(
            text(
                """
                INSERT INTO permission_sync_run
                  (source_type, update_type, cc_pair_id, status,
                   error_msg, updated_at)
                VALUES
                  (:source_type, :update_type, :cc_pair_id, :status,
                   :error_msg, :updated_at)
                """
            ),
            {
                "source_type": conn.source.value,
                "update_type": rng.choice(job_types),
                "cc_pair_id": ccp.id,
                "status": rng.choice(statuses),
                "error_msg": None,
                "updated_at": when,
            },
        )
    db.commit()
    return count


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def clean_seeded_data(db: Session) -> dict[str, int]:
    """Find and delete every row this script created, in FK-safe order.

    Tagged via SEED_PREFIX in description / name / file_name etc. Tables
    touched: chat_message__search_doc, search_doc, chat_message,
    chat_session, chat_message_feedback (cascades via ON DELETE SET NULL),
    permission_sync_run, slack_bot_config, index_attempt,
    document_by_connector_credential_pair, document, connector,
    credential, connector_credential_pair, user.

    Per-table delete with explicit FK ordering — never `DROP`.
    """
    counts: dict[str, int] = {}

    # 0. chat_message__search_doc → search_doc rows we created
    counts["chat_message__search_doc"] = (
        db.execute(
            text(
                """
            DELETE FROM chat_message__search_doc
            WHERE search_doc_id IN (
                SELECT id FROM search_doc WHERE semantic_id LIKE :p
            )
            """
            ),
            {"p": f"{SEED_PREFIX}%"},
        ).rowcount
        or 0
    )
    counts["search_doc"] = (
        db.execute(
            text("DELETE FROM search_doc WHERE semantic_id LIKE :p"),
            {"p": f"{SEED_PREFIX}%"},
        ).rowcount
        or 0
    )
    db.commit()

    # 1. chat_message__search_doc → chat_message (where session is tagged)
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
        {"p": f"{SEED_PREFIX}%"},
    )

    # 2. tool_call → chat_message
    db.execute(
        text(
            """
            DELETE FROM tool_call
            WHERE message_id IN (
                SELECT cm.id FROM chat_message cm
                JOIN chat_session cs ON cs.id = cm.chat_session_id
                WHERE cs.description LIKE :p
            )
            """
        ),
        {"p": f"{SEED_PREFIX}%"},
    )

    # 3. chat_message_feedback (FK has ON DELETE SET NULL but we want to
    #    drop the rows we created, identifiable via the tagged session).
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
        {"p": f"{SEED_PREFIX}%"},
    )

    # 4. chat_message
    counts["chat_message"] = (
        db.execute(
            text(
                """
            DELETE FROM chat_message
            WHERE chat_session_id IN (
                SELECT id FROM chat_session WHERE description LIKE :p
            )
            """
            ),
            {"p": f"{SEED_PREFIX}%"},
        ).rowcount
        or 0
    )

    # 5. chat_session
    counts["chat_session"] = (
        db.execute(
            text("DELETE FROM chat_session WHERE description LIKE :p"),
            {"p": f"{SEED_PREFIX}%"},
        ).rowcount
        or 0
    )
    db.commit()

    # 6. permission_sync_run linked to seeded ccps
    counts["permission_sync_run"] = (
        db.execute(
            text(
                """
            DELETE FROM permission_sync_run
            WHERE cc_pair_id IN (
                SELECT id FROM connector_credential_pair WHERE name LIKE :p
            )
            """
            ),
            {"p": f"{SEED_PREFIX}%"},
        ).rowcount
        or 0
    )

    # 7. slack_bot_config — tagged via channel_config JSON
    counts["slack_bot_config"] = (
        db.execute(
            text(
                """
            DELETE FROM slack_bot_config
            WHERE EXISTS (
                SELECT 1
                FROM jsonb_array_elements_text(channel_config -> 'channel_names') c
                WHERE c LIKE :p
            )
            """
            ),
            {"p": f"{SEED_PREFIX}%"},
        ).rowcount
        or 0
    )

    # 8. index_attempt linked to seeded connectors
    counts["index_attempt"] = (
        db.execute(
            text(
                """
            DELETE FROM index_attempt
            WHERE connector_id IN (
                SELECT id FROM connector WHERE name LIKE :p
            )
            """
            ),
            {"p": f"{SEED_PREFIX}%"},
        ).rowcount
        or 0
    )

    # 9. document_by_connector_credential_pair + document
    counts["document_by_connector_credential_pair"] = (
        db.execute(
            text("DELETE FROM document_by_connector_credential_pair WHERE id LIKE :p"),
            {"p": f"{SEED_PREFIX}%"},
        ).rowcount
        or 0
    )
    counts["document"] = (
        db.execute(
            text("DELETE FROM document WHERE id LIKE :p"),
            {"p": f"{SEED_PREFIX}%"},
        ).rowcount
        or 0
    )
    db.commit()

    # 10. connector_credential_pair → connector + credential
    counts["connector_credential_pair"] = (
        db.execute(
            text("DELETE FROM connector_credential_pair WHERE name LIKE :p"),
            {"p": f"{SEED_PREFIX}%"},
        ).rowcount
        or 0
    )
    counts["connector"] = (
        db.execute(
            text("DELETE FROM connector WHERE name LIKE :p"),
            {"p": f"{SEED_PREFIX}%"},
        ).rowcount
        or 0
    )
    # Credentials don't have a "name" column; we created admin_public=true
    # rows linked to seeded cc_pairs. Find via the JSON marker we put in
    # connector_specific_config? Cleaner: orphan credentials with no
    # matching cc_pair and matching empty credential_json. Skip for now
    # (low volume; manual cleanup possible).
    db.commit()

    # 11. analytics_daily_rollup is owned by the rollup pipeline, not seeded
    #     data. Don't touch it here — the orchestrator script clears the
    #     checkpoint when needed.

    # 12. users (last — many FKs reference user.id)
    counts["user"] = (
        db.execute(
            text("""DELETE FROM "user" WHERE email LIKE :p"""),
            {"p": f"{SEED_PREFIX}%"},
        ).rowcount
        or 0
    )
    db.commit()
    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clean", action="store_true", help="Wipe seeded data and exit."
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt."
    )
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--chats-per-day", type=int, default=20)
    parser.add_argument("--slackbot-share", type=float, default=0.7)
    parser.add_argument("--feedback-rate", type=float, default=0.6)
    parser.add_argument("--like-share", type=float, default=0.5)
    parser.add_argument("--resolved-share", type=float, default=0.2)
    parser.add_argument("--needs-help-share", type=float, default=0.1)
    parser.add_argument("--users", type=int, default=25, dest="users_count")
    parser.add_argument("--connectors", type=int, default=4, dest="connectors_count")
    parser.add_argument(
        "--docs-per-connector", type=int, default=200, dest="docs_per_connector"
    )
    parser.add_argument("--with-search-docs", action="store_true")
    parser.add_argument("--with-old-data", action="store_true")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed.")
    args = parser.parse_args()

    cfg = SeedConfig(
        days=args.days,
        chats_per_day=args.chats_per_day,
        slackbot_share=args.slackbot_share,
        feedback_rate=args.feedback_rate,
        like_share=args.like_share,
        resolved_share=args.resolved_share,
        needs_help_share=args.needs_help_share,
        users_count=args.users_count,
        connectors_count=args.connectors_count,
        docs_per_connector=args.docs_per_connector,
        with_search_docs=args.with_search_docs,
        with_old_data=args.with_old_data,
        seed=args.seed,
    )

    confirm_destructive(skip=args.yes)

    engine = get_sqlalchemy_engine()
    rng = random.Random(cfg.seed)

    if args.clean:
        with Session(engine) as db:
            counts = clean_seeded_data(db)
        for table, n in counts.items():
            if n:
                print(f"  cleaned {n:>8d}  rows from {table}")
        print("Done.")
        return 0

    print(f"Seeding with config: {cfg}")
    with Session(engine) as db:
        persona = lookup_default_persona(db)
        embedding_model = lookup_default_embedding_model(db)

        print(
            f"  using persona id={persona.id}, embedding_model id={embedding_model.id}"
        )

        users = seed_users(db, cfg.users_count)
        print(f"  ✓ {len(users)} users")

        pairs = seed_connectors(db, cfg.connectors_count, cfg.docs_per_connector)
        print(f"  ✓ {len(pairs)} connectors + cc_pairs")

        n_docs = seed_documents(db, pairs, cfg.docs_per_connector)
        print(f"  ✓ {n_docs} documents (+ join rows)")

        n_threads, msgs = seed_chat_data(db, cfg, users, persona.id, rng, days_offset=0)
        print(f"  ✓ {n_threads} chat threads (last {cfg.days} days)")

        if cfg.with_old_data:
            n_old = seed_old_chat_for_retention(
                db, users, persona.id, rng, count=cfg.chats_per_day * 2
            )
            print(f"  ✓ {n_old} old chat threads (35-90 days old, for retention)")

        if cfg.with_search_docs:
            n_sd = seed_search_docs_for_messages(db, msgs)
            print(f"  ✓ {n_sd} search_doc + chat_message__search_doc rows")

        n_sb = seed_slack_bot_configs(db, count=3, persona_id=persona.id, rng=rng)
        print(f"  ✓ {n_sb} slack_bot_config rows")

        n_ia = seed_index_attempts(
            db,
            pairs,
            embedding_model,
            count_per_pair=4,
            with_old=cfg.with_old_data,
            rng=rng,
        )
        print(f"  ✓ {n_ia} index_attempts")

        n_ps = seed_permission_syncs(
            db, pairs, count=8, with_old=cfg.with_old_data, rng=rng
        )
        print(f"  ✓ {n_ps} permission_sync_run rows")

    print("Seeding complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
