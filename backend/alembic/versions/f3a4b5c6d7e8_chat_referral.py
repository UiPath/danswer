"""chat_referral

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-07-05 00:00:00.000000

Adds the chat_referral table: one row per landing on the chat UI from an external
referral (e.g. a per-channel Slack "Ask Darwin" workflow), so inbound traffic can
be measured by source / channel / assistant with plain SQL.
"""
from alembic import op
import sqlalchemy as sa
import fastapi_users_db_sqlalchemy

# revision identifiers, used by Alembic.
revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_referral",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("utm_source", sa.String(), nullable=False),
        sa.Column("utm_medium", sa.String(), nullable=True),
        sa.Column("utm_campaign", sa.String(), nullable=True),
        sa.Column("utm_channel", sa.String(), nullable=True),
        sa.Column("assistant_name", sa.String(), nullable=True),
        sa.Column(
            "user_id",
            fastapi_users_db_sqlalchemy.generics.GUID(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # Traffic queries filter/group by source and time.
    op.create_index(
        "ix_chat_referral_source_created",
        "chat_referral",
        ["utm_source", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_referral_source_created", table_name="chat_referral")
    op.drop_table("chat_referral")
