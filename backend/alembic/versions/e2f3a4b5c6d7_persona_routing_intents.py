"""persona: add routing_intents (semantic intent pre-route)

Adds persona.routing_intents — admin-editable, NEWLINE-separated natural-language
intent phrases (one per line). The auto-router embeds them and routes a question
to this assistant when its nearest exemplar clears a similarity gate, BETWEEN the
keyword pre-route and the LLM router. Nullable, no backfill; blank means no
semantic override and routing is unchanged. Stored in Postgres only (never in
Vespa); embeddings are computed on demand and cached per-assistant. See
db/models.py::Persona.routing_intents and assistant_router.intent_route.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-07-01

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "persona",
        sa.Column("routing_intents", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("persona", "routing_intents")
