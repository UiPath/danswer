"""persona multilingual_query_expansion flag

Revision ID: a3f1d7c4e9b2
Revises: c8a4e2f9d1b3
Create Date: 2026-05-04 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a3f1d7c4e9b2"
down_revision = "c8a4e2f9d1b3"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "persona",
        sa.Column(
            "multilingual_query_expansion",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("persona", "multilingual_query_expansion")
