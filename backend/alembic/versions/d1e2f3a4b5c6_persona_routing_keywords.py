"""persona: add routing_keywords (deterministic keyword pre-route)

Adds persona.routing_keywords — admin-editable, comma-separated phrases that
deterministically route a question to this assistant (case-insensitive substring
match) BEFORE the LLM router runs. Nullable, no backfill; blank means no keyword
override and the LLM router decides as before. Additive — existing routing is
unchanged when no keyword matches. See db/models.py::Persona.routing_keywords and
secondary_llm_flows/assistant_router.keyword_route.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-06-30

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d1e2f3a4b5c6"
down_revision = "c0d1e2f3a4b5"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "persona",
        sa.Column("routing_keywords", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("persona", "routing_keywords")
