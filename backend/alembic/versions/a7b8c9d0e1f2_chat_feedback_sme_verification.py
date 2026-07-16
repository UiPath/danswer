"""chat_feedback SME verification

Revision ID: a7b8c9d0e1f2
Revises: f3a4b5c6d7e8
Create Date: 2026-07-07 00:00:00.000000

Adds sme_verified_by / sme_verified_at to chat_feedback so the Slack "Verified by
an SME" flow can record who verified an answer and when (extends the existing
feedback table rather than adding a new one).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "a7b8c9d0e1f2"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_feedback",
        sa.Column("sme_verified_by", sa.String(), nullable=True),
    )
    op.add_column(
        "chat_feedback",
        sa.Column("sme_verified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_feedback", "sme_verified_at")
    op.drop_column("chat_feedback", "sme_verified_by")
