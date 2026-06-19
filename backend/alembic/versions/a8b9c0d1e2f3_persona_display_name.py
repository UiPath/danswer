"""persona: add display_name (user-friendly chat label)

Adds persona.display_name — an optional, admin-editable label shown in the chat
UI. The immutable `name` stays the identifier; `display_name` is presentational
only and the chat falls back to `name` when it's blank. Backfills existing rows
with their `name` so nothing changes visually until an admin edits it. See
db/models.py::Persona.

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-06-19

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a8b9c0d1e2f3"
down_revision = "f7a8b9c0d1e2"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "persona",
        sa.Column("display_name", sa.String(), nullable=True),
    )
    # Backfill: existing assistants keep showing their current name.
    op.execute("UPDATE persona SET display_name = name WHERE display_name IS NULL")


def downgrade() -> None:
    op.drop_column("persona", "display_name")
