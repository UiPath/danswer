"""onboarding_request

Revision ID: c40f5a073fc2
Revises: 31f30b318163
Create Date: 2026-07-17 00:00:00.000000

Adds the onboarding_request table backing the self-serve team-onboarding flow
(form -> admin approval -> auto-provision + scrape). All columns additive/new
table, so no impact on existing data.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c40f5a073fc2"
down_revision = "31f30b318163"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onboarding_request",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "requester_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id"),
            nullable=True,
        ),
        sa.Column("requester_email", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "approver_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id"),
            nullable=True,
        ),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column(
            "persona_id", sa.Integer(), sa.ForeignKey("persona.id"), nullable=True
        ),
        sa.Column(
            "document_set_id",
            sa.Integer(),
            sa.ForeignKey("document_set.id"),
            nullable=True,
        ),
        sa.Column(
            "slack_bot_config_id",
            sa.Integer(),
            sa.ForeignKey("slack_bot_config.id"),
            nullable=True,
        ),
        sa.Column("cc_pair_ids", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("onboarding_request")
