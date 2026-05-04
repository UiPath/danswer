"""Performance indexes for chat UI hot paths

Adds two FK-column indexes that the chat UI hits on every render. Both
columns are FKs in the schema but neither was indexed (Postgres doesn't
auto-index FK columns), so each chat-open / sidebar-list did a full
sequential scan.

- chat_message(chat_session_id): every "open chat session" page hits
  `WHERE chat_session_id = :id ORDER BY parent_message NULLS FIRST`
  (db/chat.py::get_chat_messages_by_session). Without this index that's
  a seq scan of every chat_message ever stored. Lazy-loaded relationships
  (chat_message.tool_calls, .chat_message_feedbacks) make it worse.

- chat_session(user_id): the chat-history sidebar hits
  `WHERE user_id = :user_id` (db/chat.py::get_chat_sessions_by_user) on
  every chat UI load.

Both created CONCURRENTLY so production deployments don't take a write
lock on chat_message / chat_session during the build. CONCURRENTLY can't
run inside the migration's wrapping transaction, hence
`with op.get_context().autocommit_block()`.

Revision ID: b5d3f1a9e7c2
Revises: fd307e9ecc9b
Create Date: 2026-05-01

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "b5d3f1a9e7c2"
down_revision = "fd307e9ecc9b"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    # Raw SQL to get both CONCURRENTLY and IF NOT EXISTS in one shot —
    # `op.create_index` doesn't expose IF NOT EXISTS as a top-level
    # kwarg, and passing `if_not_exists=` produces a SAWarning. The
    # autocommit_block exits the migration's wrapping transaction so
    # CONCURRENTLY is allowed.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_chat_message_chat_session_id "
            "ON chat_message USING btree (chat_session_id)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_chat_session_user_id "
            "ON chat_session USING btree (user_id)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chat_session_user_id")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chat_message_chat_session_id")
