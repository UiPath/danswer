"""index_attempt.checkpoint

Revision ID: ckpt_idx_attempt_01
Revises: a1b2c3d4e5f6
Create Date: 2026-07-22 00:00:00.000000

Adds `checkpoint`: opaque JSON persisted by a CheckpointedConnector so a run
killed mid-crawl resumes from where it left off instead of restarting (e.g.
SharePoint's Graph delta `@odata.nextLink`/`@odata.deltaLink` cursor). Additive
nullable column — NULL for every existing row and for connectors that don't
implement checkpointing, so no behavior change for them.

NOTE: a non-hex revision id is used intentionally — the earlier hex id collided
with an existing migration (the ids in this tree follow a rolling-hex pattern).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "ckpt_idx_attempt_01"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "index_attempt",
        sa.Column("checkpoint", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("index_attempt", "checkpoint")
