"""Daily analytics rollup table

Persists pre-aggregated daily metrics so the admin analytics page
survives chat retention deletes. See `db/models.py::AnalyticsDailyRollup`
and `db/analytics_rollup.py` for the runtime contract.

Date is the primary key — one row per UTC day. No FK to chat_message /
chat_session on purpose, so retention sweeps don't cascade into this
table. Indefinite retention.

Revision ID: c8a4e2f9d1b3
Revises: b5d3f1a9e7c2
Create Date: 2026-05-01

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c8a4e2f9d1b3"
down_revision = "b5d3f1a9e7c2"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "analytics_daily_rollup",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column(
            "total_queries",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_likes",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_dislikes",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_resolved",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_needs_help",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "active_users",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "slackbot_total",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "slackbot_auto_resolved",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "rolled_up_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("date", name="analytics_daily_rollup_pkey"),
    )


def downgrade() -> None:
    op.drop_table("analytics_daily_rollup")
