"""Analytics per-assistant daily stats table

Durable per-assistant-per-day chat activity counts so the "most-used
assistants" leaderboard (and an approximate datasets-in-use view derived
via persona__document_set) survives chat retention and spans full history.
Upserted daily by the rollup BEFORE the retention sweep (see
db/models.py::AnalyticsPersonaDailyStats and db/analytics_rollup.py).

Composite PK (persona_id, date). No FK to `persona` — name joined live, so
a deleted assistant drops off without erasing history.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-31

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "analytics_persona_daily_stats",
        sa.Column("persona_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("session_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("like_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("dislike_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "rolled_up_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("persona_id", "date"),
    )
    op.create_index(
        "ix_analytics_persona_daily_stats_date",
        "analytics_persona_daily_stats",
        ["date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analytics_persona_daily_stats_date",
        table_name="analytics_persona_daily_stats",
    )
    op.drop_table("analytics_persona_daily_stats")
