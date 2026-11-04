"""answer_trace: operator-facing per-answer provenance table

One row per answer (chat or search). Filterable dimensions are columns; the
ordered per-stage detail is in the `events` JSONB. Written on-by-default; a
retention job prunes rows older than N days (default 14). See
danswer/answer_trace/ and db/models.py::AnswerTrace.

Revision ID: answer_trace_01
Revises: deeprag0001
Create Date: 2026-07-27

"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision = "answer_trace_01"
down_revision = "deeprag0001"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "answer_trace",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trace_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chat_message_id", sa.Integer(), nullable=True),
        sa.Column("chat_session_id", sa.Integer(), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("surface", sa.String(), nullable=False),
        sa.Column("persona_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("rephrased_query", sa.Text(), nullable=True),
        sa.Column("route_stage", sa.String(), nullable=True),
        sa.Column("route_confidence", sa.Float(), nullable=True),
        sa.Column("retrieved_count", sa.Integer(), nullable=True),
        sa.Column("reranked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("cited_source_count", sa.Integer(), nullable=True),
        sa.Column(
            "deep_rag_enabled", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column(
            "deep_rag_escalated",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("sub_question_count", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("total_duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "trace_version", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "events",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["chat_message_id"], ["chat_message.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_message_id"),
    )
    op.create_index(
        "ix_answer_trace_trace_uuid", "answer_trace", ["trace_uuid"], unique=True
    )
    op.create_index("ix_answer_trace_session", "answer_trace", ["chat_session_id"])
    op.create_index("ix_answer_trace_user", "answer_trace", ["user_id"])
    op.create_index("ix_answer_trace_created", "answer_trace", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_answer_trace_created", table_name="answer_trace")
    op.drop_index("ix_answer_trace_user", table_name="answer_trace")
    op.drop_index("ix_answer_trace_session", table_name="answer_trace")
    op.drop_index("ix_answer_trace_trace_uuid", table_name="answer_trace")
    op.drop_table("answer_trace")
