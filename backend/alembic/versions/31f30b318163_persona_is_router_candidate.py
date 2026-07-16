"""persona is_router_candidate

Revision ID: 31f30b318163
Revises: a7b8c9d0e1f2
Create Date: 2026-07-16 00:00:00.000000

Adds `is_router_candidate` to persona so an admin can exclude an assistant from
the auto-routed Search tab (both the keyword route and the kNN fallback) while
keeping it manually selectable. NOT NULL with server_default true, so existing
rows keep participating in routing (no behavior change on upgrade).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "31f30b318163"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "persona",
        sa.Column(
            "is_router_candidate",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
    )


def downgrade() -> None:
    op.drop_column("persona", "is_router_candidate")
