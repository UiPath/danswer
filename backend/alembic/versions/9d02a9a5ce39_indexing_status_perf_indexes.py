"""Performance indexes for /admin/connector/indexing-status

Adds covering indexes to the two hot lookup paths used by the connector
indexing-status route:

- (task_name, id DESC) on task_queue_jobs — used by `get_latest_task` and
  the new bulk `get_latest_tasks_by_names`.
- (connector_id, credential_id, embedding_model_id, time_created DESC)
  on index_attempt — used by `get_last_attempt` and the bulk
  `get_latest_index_attempts` group-by/max pattern.

These speed up "latest-per-group" queries from full-table scans to
index-only seeks. CONCURRENTLY would be safer in production, but Alembic
migrations run inside a transaction by default; the regular CREATE INDEX
is fine for dev / smaller deployments. Switch to CONCURRENTLY by hand if
the live tables are large enough that an exclusive write lock matters.

Revision ID: 9d02a9a5ce39
Revises: 792d1af3dc44
Create Date: 2026-05-01

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "9d02a9a5ce39"
down_revision = "792d1af3dc44"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_index(
        "ix_task_queue_jobs_name_id",
        "task_queue_jobs",
        ["task_name", "id"],
        unique=False,
        postgresql_using="btree",
    )
    op.create_index(
        "ix_index_attempt_pair_model_time",
        "index_attempt",
        [
            "connector_id",
            "credential_id",
            "embedding_model_id",
            "time_created",
        ],
        unique=False,
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index("ix_index_attempt_pair_model_time", table_name="index_attempt")
    op.drop_index("ix_task_queue_jobs_name_id", table_name="task_queue_jobs")
