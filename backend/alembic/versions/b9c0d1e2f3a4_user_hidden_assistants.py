"""user: add hidden_assistants (opt-out assistant visibility)

Adds user.hidden_assistants — the list of assistant (persona) ids a user has
explicitly hidden from their chat picker. This flips assistant visibility from
opt-IN (only assistants in `chosen_assistants` were shown) to opt-OUT: every
accessible assistant is visible by default, so a newly created admin assistant
appears for all users automatically; a user hides the ones they don't want.

`chosen_assistants` now controls ORDER/default only, not visibility.

No backfill: the chat experience hasn't been rolled out to end users yet, so
there is no curated state to preserve — every existing user simply starts with
an empty hidden list (= sees everything), which is the desired behavior. See
db/models.py::User.

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-06-21

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "b9c0d1e2f3a4"
down_revision = "a8b9c0d1e2f3"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "hidden_assistants",
            postgresql.ARRAY(sa.Integer()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("user", "hidden_assistants")
