"""slack bot: response blocklist (suppress responses for certain senders)

Creates slack_bot_response_blocklist — senders (by email) whose Slack messages
should NOT trigger a Darwin response. DB-driven so the list can change without a
redeploy. Seeds the first entry (jr.bancel@uipath.com). See
db/models.py::SlackBotResponseBlocklist and
danswerbot/slack/handlers/handle_message.py.

Revision ID: f7a8b9c0d1e2
Revises: f6a7b8c9d0e1
Create Date: 2026-06-17

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f7a8b9c0d1e2"
down_revision = "f6a7b8c9d0e1"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "slack_bot_response_blocklist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Single unique index — mirrors `mapped_column(String, unique=True, index=True)`.
    op.create_index(
        op.f("ix_slack_bot_response_blocklist_email"),
        "slack_bot_response_blocklist",
        ["email"],
        unique=True,
    )

    # Seed the initial blocked senders (stored lowercase; matched
    # case-insensitively). Further additions are plain DB inserts — no migration.
    op.execute(
        sa.text(
            "INSERT INTO slack_bot_response_blocklist (email) VALUES "
            "('jr.bancel@uipath.com'), ('andrei.barbu@uipath.com') "
            "ON CONFLICT (email) DO NOTHING"
        )
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_slack_bot_response_blocklist_email"),
        table_name="slack_bot_response_blocklist",
    )
    op.drop_table("slack_bot_response_blocklist")
