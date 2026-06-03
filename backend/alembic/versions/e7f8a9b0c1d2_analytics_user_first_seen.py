"""Analytics user-first-seen table (chat adoption curve)

Durable per-user "first date this user used chat" aggregate so the
adoption curve on the admin Analytics page survives chat retention
deletes. Populated incrementally by the rollup BEFORE the retention sweep
(see db/models.py::AnalyticsUserFirstSeen and db/analytics_rollup.py).

No FK to `user` on purpose — deleting a user must not erase the historical
fact that they once adopted chat, nor cascade into this aggregate (mirrors
analytics_daily_rollup).

Revision ID: e7f8a9b0c1d2
Revises: c8a4e2f9d1b3
Create Date: 2026-05-31

"""
from alembic import op
import sqlalchemy as sa
import fastapi_users_db_sqlalchemy


# revision identifiers, used by Alembic.
revision = "e7f8a9b0c1d2"
down_revision = "c8a4e2f9d1b3"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "analytics_user_first_seen",
        sa.Column(
            "user_id", fastapi_users_db_sqlalchemy.generics.GUID(), nullable=False
        ),
        sa.Column("first_seen_date", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        "ix_analytics_user_first_seen_first_seen_date",
        "analytics_user_first_seen",
        ["first_seen_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analytics_user_first_seen_first_seen_date",
        table_name="analytics_user_first_seen",
    )
    op.drop_table("analytics_user_first_seen")
