"""onboarding_request.help_thread_ts

Revision ID: a1b2c3d4e5f6
Revises: c40f5a073fc2
Create Date: 2026-07-18 00:00:00.000000

Adds help_thread_ts: the Slack ts of the customer-facing root message posted on
submit, so lifecycle updates (approved / complete / failed) can be posted as
replies in that thread. Additive nullable column — no impact on existing rows.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "c40f5a073fc2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "onboarding_request",
        sa.Column("help_thread_ts", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("onboarding_request", "help_thread_ts")
