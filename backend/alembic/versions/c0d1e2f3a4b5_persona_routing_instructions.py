"""persona: add routing_instructions (router-only metadata)

Adds persona.routing_instructions — admin-editable free-text guidance read ONLY
by the auto-routed Search tab's assistant router (which assistant to pick for a
question). It is NEVER rendered in the user-facing UI; `description` stays the
short human-facing label. Nullable with no backfill — the router falls back to
`description` when this is blank, so existing assistants route on what they have
today until an admin fills this in. See db/models.py::Persona.routing_instructions
and secondary_llm_flows/assistant_router.

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-06-29

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c0d1e2f3a4b5"
down_revision = "b9c0d1e2f3a4"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "persona",
        sa.Column("routing_instructions", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("persona", "routing_instructions")
