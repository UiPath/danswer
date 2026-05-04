"""Per-attempt indexing priority

Adds `indexing_priority` to `index_attempt` so a single queued attempt can
jump the line ahead of other NOT_STARTED attempts (including others for
the same connector / cc-pair) without affecting any persistent connector
config. Default 0; conventional manual ceiling is 10.

Also adds a helper index on (status, indexing_priority DESC, time_created)
so the scheduler's "pick the next attempt to dispatch" query stays fast
even with many queued rows.

Revision ID: fd307e9ecc9b
Revises: 9d02a9a5ce39
Create Date: 2026-05-01

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "fd307e9ecc9b"
down_revision = "9d02a9a5ce39"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "index_attempt",
        sa.Column(
            "indexing_priority",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    # Speeds up `get_not_started_index_attempts` which now filters by
    # status='NOT_STARTED' and orders by indexing_priority DESC, time_created ASC.
    op.create_index(
        "ix_index_attempt_status_priority_time",
        "index_attempt",
        ["status", "indexing_priority", "time_created"],
        unique=False,
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index("ix_index_attempt_status_priority_time", table_name="index_attempt")
    op.drop_column("index_attempt", "indexing_priority")
