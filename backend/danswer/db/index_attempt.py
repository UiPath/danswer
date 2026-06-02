from collections.abc import Sequence

from sqlalchemy import and_
from sqlalchemy import ColumnElement
from sqlalchemy import delete
from sqlalchemy import desc
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import Select
from sqlalchemy import text
from sqlalchemy import update
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import Session

from danswer.db.models import EmbeddingModel
from danswer.db.models import IndexAttempt
from danswer.db.models import IndexingStatus
from danswer.db.models import IndexModelStatus
from danswer.server.documents.models import ConnectorCredentialPairIdentifier
from danswer.utils.logger import setup_logger
from danswer.utils.telemetry import optional_telemetry
from danswer.utils.telemetry import RecordType


# ---------------------------------------------------------------------------
# Per-cc-pair indexing lock (Postgres advisory lock)
# ---------------------------------------------------------------------------
#
# When NUM_INDEXING_WORKERS > 1, two indexing attempts for the same
# (connector_id, credential_id) could otherwise be assigned to two Dask
# workers and run concurrently — racing on Vespa writes, on
# `last_successful_index_time`, and on connector-side checkpoint state.
#
# Upstream Onyx prevents this with per-cc-pair Redis fences. We don't
# have Redis (the broker is Postgres), but advisory locks give the same
# semantics: a lock acquired by one session is invisible to other
# sessions until released or the holding session disconnects.
#
# `pg_try_advisory_lock(int8)` is the non-blocking form — we want a
# fast-fail "someone else has it" signal, not to wait. Lock IDs are 64-
# bit; we encode the cc-pair as `(offset || hash(connector_id, credential_id))`
# so they don't collide with the retention sweep's lock id.
_INDEXING_LOCK_KEY_OFFSET = 0x494E4458_00000000  # b"INDX" in the high bits


def _cc_pair_lock_key(connector_id: int, credential_id: int) -> int:
    """Stable 64-bit advisory-lock key for (connector_id, credential_id).

    The high 32 bits are a fixed offset (`b"INDX"`) so this lock id can
    never collide with the retention sweep's lock id (`b"RETENTIO"`) or
    any other future advisory lock. The low 32 bits are a hash of the
    cc-pair tuple — collisions there mean two unrelated cc-pairs would
    share a lock, but with 32 bits of space and typical cc-pair counts in
    the hundreds the birthday bound is well below 1%.
    """
    h = (connector_id * 0x9E3779B1 ^ credential_id) & 0xFFFFFFFF
    # Combine into a signed 64-bit int (Postgres bigint range).
    raw = _INDEXING_LOCK_KEY_OFFSET | h
    if raw >= 1 << 63:
        raw -= 1 << 64
    return raw


def try_acquire_cc_pair_lock(
    db_session: Session, connector_id: int, credential_id: int
) -> bool:
    """Non-blocking attempt to acquire the indexing lock for this cc-pair.

    Returns True if the lock was acquired (caller is now responsible for
    eventually calling `release_cc_pair_lock`). Returns False if another
    session already holds it.

    The lock is *session-scoped*: it survives commits but is automatically
    released when the database session disconnects. So even if a worker
    process crashes mid-run without calling release, the lock won't be
    permanently stuck once the connection times out.
    """
    key = _cc_pair_lock_key(connector_id, credential_id)
    row = db_session.execute(
        text("SELECT pg_try_advisory_lock(:k)"), {"k": key}
    ).scalar()
    return bool(row)


def release_cc_pair_lock(
    db_session: Session, connector_id: int, credential_id: int
) -> None:
    """Release the indexing lock for this cc-pair. Safe to call even if
    we don't currently hold the lock — Postgres returns false but doesn't
    raise."""
    key = _cc_pair_lock_key(connector_id, credential_id)
    db_session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


logger = setup_logger()


def get_index_attempt(
    db_session: Session, index_attempt_id: int
) -> IndexAttempt | None:
    stmt = select(IndexAttempt).where(IndexAttempt.id == index_attempt_id)
    return db_session.scalars(stmt).first()


def create_index_attempt(
    connector_id: int,
    credential_id: int,
    embedding_model_id: int,
    db_session: Session,
    from_beginning: bool = False,
    indexing_priority: int = 0,
) -> int:
    new_attempt = IndexAttempt(
        connector_id=connector_id,
        credential_id=credential_id,
        embedding_model_id=embedding_model_id,
        from_beginning=from_beginning,
        status=IndexingStatus.NOT_STARTED,
        indexing_priority=max(0, min(int(indexing_priority), 100)),
    )
    db_session.add(new_attempt)
    db_session.commit()

    return new_attempt.id


def update_index_attempt_priority(
    index_attempt_id: int,
    indexing_priority: int,
    db_session: Session,
) -> IndexAttempt | None:
    """Update the priority of a NOT_STARTED attempt. Returns None if the
    attempt doesn't exist or is no longer in NOT_STARTED — once an attempt
    has been dispatched the priority can't change its scheduling decision."""
    attempt = db_session.execute(
        select(IndexAttempt).where(IndexAttempt.id == index_attempt_id)
    ).scalar_one_or_none()
    if attempt is None:
        return None
    if attempt.status != IndexingStatus.NOT_STARTED:
        return None
    attempt.indexing_priority = max(0, min(int(indexing_priority), 100))
    db_session.commit()
    return attempt


def get_inprogress_index_attempts(
    connector_id: int | None,
    db_session: Session,
) -> list[IndexAttempt]:
    stmt = select(IndexAttempt)
    if connector_id is not None:
        stmt = stmt.where(IndexAttempt.connector_id == connector_id)
    stmt = stmt.where(IndexAttempt.status == IndexingStatus.IN_PROGRESS)

    incomplete_attempts = db_session.scalars(stmt)
    return list(incomplete_attempts.all())


def get_not_started_index_attempts(db_session: Session) -> list[IndexAttempt]:
    """This eagerly loads the connector and credential so that the db_session can be expired
    before running long-living indexing jobs, which causes increasing memory usage.

    Higher-priority attempts come first; within the same priority band the
    oldest attempt wins (FIFO) so we don't starve normal-priority queues.
    """
    stmt = select(IndexAttempt)
    stmt = stmt.where(IndexAttempt.status == IndexingStatus.NOT_STARTED)
    stmt = stmt.order_by(
        desc(IndexAttempt.indexing_priority),
        IndexAttempt.time_created.asc(),
    )
    stmt = stmt.options(
        joinedload(IndexAttempt.connector), joinedload(IndexAttempt.credential)
    )
    new_attempts = db_session.scalars(stmt)
    return list(new_attempts.all())


def mark_attempt_in_progress__no_commit(
    index_attempt: IndexAttempt,
) -> None:
    index_attempt.status = IndexingStatus.IN_PROGRESS
    index_attempt.time_started = index_attempt.time_started or func.now()  # type: ignore


def mark_attempt_succeeded(
    index_attempt: IndexAttempt,
    db_session: Session,
) -> None:
    index_attempt.status = IndexingStatus.SUCCESS
    db_session.add(index_attempt)
    db_session.commit()


def mark_attempt_failed(
    index_attempt: IndexAttempt,
    db_session: Session,
    failure_reason: str = "Unknown",
    full_exception_trace: str | None = None,
) -> None:
    index_attempt.status = IndexingStatus.FAILED
    index_attempt.error_msg = failure_reason
    index_attempt.full_exception_trace = full_exception_trace
    db_session.add(index_attempt)
    db_session.commit()

    source = index_attempt.connector.source
    optional_telemetry(record_type=RecordType.FAILURE, data={"connector": source})


def update_docs_indexed(
    db_session: Session,
    index_attempt: IndexAttempt,
    total_docs_indexed: int,
    new_docs_indexed: int,
    docs_removed_from_index: int,
) -> None:
    index_attempt.total_docs_indexed = total_docs_indexed
    index_attempt.new_docs_indexed = new_docs_indexed
    index_attempt.docs_removed_from_index = docs_removed_from_index

    db_session.add(index_attempt)
    db_session.commit()


def get_last_attempt(
    connector_id: int,
    credential_id: int,
    embedding_model_id: int | None,
    db_session: Session,
) -> IndexAttempt | None:
    stmt = select(IndexAttempt).where(
        IndexAttempt.connector_id == connector_id,
        IndexAttempt.credential_id == credential_id,
        IndexAttempt.embedding_model_id == embedding_model_id,
    )
    # Note, the below is using time_created instead of time_updated
    stmt = stmt.order_by(desc(IndexAttempt.time_created))

    # LIMIT 1 in SQL — NOT just Result.first(). `execute(stmt).scalars().first()`
    # does not add a LIMIT, so without this the DB returns the cc-pair's ENTIRE
    # attempt history (psycopg2 buffers it all client-side, the ORM materializes
    # every row) and we throw all but one away. The indexing scheduler calls this
    # once per cc-pair every loop, so with a large index_attempt table that spiked
    # the scheduler to multi-GB per cycle (OOMKilled). With LIMIT 1 the DB returns
    # one row. See update.py::create_indexing_jobs.
    stmt = stmt.limit(1)

    return db_session.execute(stmt).scalars().first()


def get_latest_index_attempts(
    connector_credential_pair_identifiers: list[ConnectorCredentialPairIdentifier],
    secondary_index: bool,
    db_session: Session,
) -> Sequence[IndexAttempt]:
    ids_stmt = select(
        IndexAttempt.connector_id,
        IndexAttempt.credential_id,
        func.max(IndexAttempt.time_created).label("max_time_created"),
    ).join(EmbeddingModel, IndexAttempt.embedding_model_id == EmbeddingModel.id)

    if secondary_index:
        ids_stmt = ids_stmt.where(EmbeddingModel.status == IndexModelStatus.FUTURE)
    else:
        ids_stmt = ids_stmt.where(EmbeddingModel.status == IndexModelStatus.PRESENT)

    where_stmts: list[ColumnElement] = []
    for connector_credential_pair_identifier in connector_credential_pair_identifiers:
        where_stmts.append(
            and_(
                IndexAttempt.connector_id
                == connector_credential_pair_identifier.connector_id,
                IndexAttempt.credential_id
                == connector_credential_pair_identifier.credential_id,
            )
        )
    if where_stmts:
        ids_stmt = ids_stmt.where(or_(*where_stmts))
    ids_stmt = ids_stmt.group_by(IndexAttempt.connector_id, IndexAttempt.credential_id)
    ids_subqery = ids_stmt.subquery()

    stmt = (
        select(IndexAttempt)
        .join(
            ids_subqery,
            and_(
                ids_subqery.c.connector_id == IndexAttempt.connector_id,
                ids_subqery.c.credential_id == IndexAttempt.credential_id,
            ),
        )
        .where(IndexAttempt.time_created == ids_subqery.c.max_time_created)
    )

    return db_session.execute(stmt).scalars().all()


def get_index_attempts_for_cc_pair(
    db_session: Session,
    cc_pair_identifier: ConnectorCredentialPairIdentifier,
    only_current: bool = True,
    disinclude_finished: bool = False,
    limit: int | None = None,
) -> Sequence[IndexAttempt]:
    # `limit` is optional and defaults to None (unbounded — unchanged behavior).
    # IndexAttempt rows carry large Text columns (error_msg, full_exception_trace),
    # so callers that only need existence or a recent slice should pass a limit
    # rather than materialize a busy cc-pair's entire history.
    stmt = select(IndexAttempt).where(
        and_(
            IndexAttempt.connector_id == cc_pair_identifier.connector_id,
            IndexAttempt.credential_id == cc_pair_identifier.credential_id,
        )
    )
    if disinclude_finished:
        stmt = stmt.where(
            IndexAttempt.status.in_(
                [IndexingStatus.NOT_STARTED, IndexingStatus.IN_PROGRESS]
            )
        )
    if only_current:
        stmt = stmt.join(EmbeddingModel).where(
            EmbeddingModel.status == IndexModelStatus.PRESENT
        )

    stmt = stmt.order_by(IndexAttempt.time_created.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return db_session.execute(stmt).scalars().all()


def _cc_pair_index_attempts_base_stmt(
    cc_pair_identifier: ConnectorCredentialPairIdentifier,
    only_current: bool,
) -> Select:
    """Shared WHERE/JOIN for the cc-pair index-attempt queries (count +
    paginated fetch) so they always agree on what counts as 'in scope'."""
    stmt = select(IndexAttempt).where(
        and_(
            IndexAttempt.connector_id == cc_pair_identifier.connector_id,
            IndexAttempt.credential_id == cc_pair_identifier.credential_id,
        )
    )
    if only_current:
        stmt = stmt.join(EmbeddingModel).where(
            EmbeddingModel.status == IndexModelStatus.PRESENT
        )
    return stmt


def count_index_attempts_for_cc_pair(
    db_session: Session,
    cc_pair_identifier: ConnectorCredentialPairIdentifier,
    only_current: bool = True,
) -> int:
    base = _cc_pair_index_attempts_base_stmt(cc_pair_identifier, only_current)
    count_stmt = select(func.count()).select_from(base.subquery())
    return db_session.execute(count_stmt).scalar_one()


def get_paginated_index_attempts_for_cc_pair(
    db_session: Session,
    cc_pair_identifier: ConnectorCredentialPairIdentifier,
    page: int,
    page_size: int,
    only_current: bool = True,
) -> Sequence[IndexAttempt]:
    """One page of a cc-pair's index attempts, newest first. `page` is 0-based.
    Server-side LIMIT/OFFSET so the API never materializes the full history."""
    stmt = _cc_pair_index_attempts_base_stmt(cc_pair_identifier, only_current)
    stmt = stmt.order_by(IndexAttempt.time_created.desc())
    stmt = stmt.limit(page_size).offset(max(page, 0) * page_size)
    return db_session.execute(stmt).scalars().all()


def delete_index_attempts(
    connector_id: int,
    credential_id: int,
    db_session: Session,
) -> None:
    stmt = delete(IndexAttempt).where(
        IndexAttempt.connector_id == connector_id,
        IndexAttempt.credential_id == credential_id,
    )
    db_session.execute(stmt)


def expire_index_attempts(
    embedding_model_id: int,
    db_session: Session,
) -> None:
    delete_query = (
        delete(IndexAttempt)
        .where(IndexAttempt.embedding_model_id == embedding_model_id)
        .where(IndexAttempt.status == IndexingStatus.NOT_STARTED)
    )
    db_session.execute(delete_query)

    update_query = (
        update(IndexAttempt)
        .where(IndexAttempt.embedding_model_id == embedding_model_id)
        .where(IndexAttempt.status != IndexingStatus.SUCCESS)
        .values(
            status=IndexingStatus.FAILED,
            error_msg="Canceled due to embedding model swap",
        )
    )
    db_session.execute(update_query)

    db_session.commit()


def cancel_indexing_attempts_for_connector(
    connector_id: int,
    db_session: Session,
    include_secondary_index: bool = False,
) -> None:
    stmt = delete(IndexAttempt).where(
        IndexAttempt.connector_id == connector_id,
        IndexAttempt.status == IndexingStatus.NOT_STARTED,
    )

    if not include_secondary_index:
        subquery = select(EmbeddingModel.id).where(
            EmbeddingModel.status != IndexModelStatus.FUTURE
        )
        stmt = stmt.where(IndexAttempt.embedding_model_id.in_(subquery))

    db_session.execute(stmt)

    db_session.commit()


def cancel_indexing_attempts_past_model(
    db_session: Session,
) -> None:
    db_session.execute(
        update(IndexAttempt)
        .where(
            IndexAttempt.status.in_(
                [IndexingStatus.IN_PROGRESS, IndexingStatus.NOT_STARTED]
            ),
            IndexAttempt.embedding_model_id == EmbeddingModel.id,
            EmbeddingModel.status == IndexModelStatus.PAST,
        )
        .values(status=IndexingStatus.FAILED)
    )

    db_session.commit()


def count_unique_cc_pairs_with_successful_index_attempts(
    embedding_model_id: int | None,
    db_session: Session,
) -> int:
    """Collect all of the Index Attempts that are successful and for the specified embedding model
    Then do distinct by connector_id and credential_id which is equivalent to the cc-pair. Finally,
    do a count to get the total number of unique cc-pairs with successful attempts"""
    unique_pairs_count = (
        db_session.query(IndexAttempt.connector_id, IndexAttempt.credential_id)
        .filter(
            IndexAttempt.embedding_model_id == embedding_model_id,
            IndexAttempt.status == IndexingStatus.SUCCESS,
        )
        .distinct()
        .count()
    )

    return unique_pairs_count
