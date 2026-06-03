"""document: add indexed_content_hash (skip re-index of unchanged content)

Stores the sha256 of a document's indexed content as of the last successful
Vespa write. The indexing pipeline skips the expensive Vespa clear-and-rewrite
when a connector re-emits a document whose content is unchanged even though its
doc_updated_at advanced (e.g. Salesforce LastModifiedDate churn re-pulling the
whole corpus every poll). Nullable: existing rows fall back to the
doc_updated_at skip until they're next indexed. See
db/models.py::Document and indexing/indexing_pipeline.py::get_doc_ids_to_update.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-03

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "document",
        sa.Column("indexed_content_hash", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document", "indexed_content_hash")
