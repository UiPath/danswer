"""file_store: add object_key, make lobj_oid nullable (object-store backend)

Lets the file_store table locate bytes either in a Postgres large object
(lobj_oid) OR an object-storage blob (object_key). Both nullable so
PostgresBackedFileStore and AzureBlobFileStore coexist during migration.
See db/models.py::PGFileStore and file_store/file_store.py.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-01

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "file_store", sa.Column("object_key", sa.String(), nullable=True)
    )
    op.alter_column("file_store", "lobj_oid", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # NOTE: only safe if no rows rely on object_key (all bytes back in lobjs).
    op.alter_column(
        "file_store", "lobj_oid", existing_type=sa.Integer(), nullable=False
    )
    op.drop_column("file_store", "object_key")
