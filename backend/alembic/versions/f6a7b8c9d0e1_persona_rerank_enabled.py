"""persona: add rerank_enabled (per-assistant cross-encoder reranking opt-in)

Per-assistant toggle for cross-encoder reranking. Only takes effect when
reranking is globally available (RERANK_ENABLED + a GPU-backed model server);
default false so existing assistants and the GPU-free local/default setup are
unchanged. Lets reranking be rolled out incrementally / A-B compared per
assistant before becoming the default. See db/models.py::Persona and
search/preprocessing/preprocessing.py.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-03

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "persona",
        sa.Column(
            "rerank_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("persona", "rerank_enabled")
