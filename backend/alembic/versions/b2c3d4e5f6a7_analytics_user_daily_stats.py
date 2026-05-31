"""Analytics per-user daily stats table (durable leaderboard source)

Durable per-user-per-day chat activity counts so the "top users by
activity" leaderboard survives chat retention and spans full history.
Upserted daily by the rollup BEFORE the retention sweep (see
db/models.py::AnalyticsUserDailyStats and db/analytics_rollup.py).

Composite PK (user_id, date). No FK to `user` — email is joined live, so
a deleted user drops off the leaderboard without erasing history.

Revision ID: b2c3d4e5f6a7
Revises: e7f8a9b0c1d2
Create Date: 2026-05-31

"""
from alembic import op
import sqlalchemy as sa
import fastapi_users_db_sqlalchemy


# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a7"
down_revision = "e7f8a9b0c1d2"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "analytics_user_daily_stats",
        sa.Column(
            "user_id", fastapi_users_db_sqlalchemy.generics.GUID(), nullable=False
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("like_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("dislike_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "rolled_up_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "date"),
    )
    op.create_index(
        "ix_analytics_user_daily_stats_date",
        "analytics_user_daily_stats",
        ["date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analytics_user_daily_stats_date",
        table_name="analytics_user_daily_stats",
    )
    op.drop_table("analytics_user_daily_stats")
