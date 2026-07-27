"""persona: add deep_rag_enabled (per-assistant Deep RAG opt-in)

Per-assistant toggle for feature-gated Deep RAG (verdict-gated multi-hop). Only
takes effect when the chat request also sets the deep_rag override; default false
so existing assistants are unchanged until piloted. Lets Deep RAG be rolled out
one assistant at a time. See db/models.py::Persona and deep_rag/controller.py.

Revision ID: deeprag0001
Revises: ckpt_idx_attempt_01
Create Date: 2026-07-27

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "deeprag0001"
# Re-parented onto the SharePoint checkpoint migration (was a1b2c3d4e5f6) after
# feature/darwin merged the SharePoint PR, so this stays a single linear head.
down_revision = "ckpt_idx_attempt_01"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "persona",
        sa.Column(
            "deep_rag_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("persona", "deep_rag_enabled")
