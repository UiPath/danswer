"""persona: add shared_instructions_enabled (per-assistant opt-out of the global
Shared Assistant Instructions)

The Admin → Settings `shared_prompt_instructions` text is appended to every
assistant's system prompt at answer time. This per-assistant flag lets an
assistant opt OUT of that injection; default TRUE so the global policy applies
everywhere unless deliberately disabled in the assistant editor. See
db/models.py::Persona and llm/answering/models.py::with_shared_instructions.

Revision ID: sharedinstr01
Revises: answer_trace_01
Create Date: 2026-07-31

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "sharedinstr01"
down_revision = "answer_trace_01"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "persona",
        sa.Column(
            "shared_instructions_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("persona", "shared_instructions_enabled")
