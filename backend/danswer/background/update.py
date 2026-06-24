import logging
import os
import time
from datetime import datetime
from typing import Any

import dask
from dask.distributed import Client
from dask.distributed import Future
from distributed import LocalCluster
from sqlalchemy.orm import Session

from danswer.background.indexing.dask_utils import ResourceLogger
from danswer.background.indexing.job_client import SimpleJob
from danswer.background.indexing.job_client import SimpleJobClient
from danswer.background.indexing.run_indexing import run_indexing_entrypoint
from danswer.configs.app_configs import CLEANUP_INDEXING_JOBS_TIMEOUT
from danswer.configs.app_configs import DASK_JOB_CLIENT_ENABLED
from danswer.configs.app_configs import DISABLE_INDEX_UPDATE_ON_SWAP
from danswer.configs.app_configs import NUM_INDEXING_WORKERS
from danswer.configs.indexing_concurrency import cap_for_source
from danswer.configs.indexing_concurrency import PER_SOURCE_CAP
from danswer.configs.indexing_concurrency import PER_SOURCE_CAP_OVERRIDES
from danswer.db.connector import fetch_connectors
from danswer.db.embedding_model import get_current_db_embedding_model
from danswer.db.embedding_model import get_secondary_db_embedding_model
from danswer.db.engine import get_db_current_time
from danswer.db.engine import get_sqlalchemy_engine
from danswer.db.index_attempt import create_index_attempt
from danswer.db.index_attempt import get_index_attempt
from danswer.db.index_attempt import get_inprogress_index_attempts
from danswer.db.index_attempt import get_last_attempt
from danswer.db.index_attempt import get_not_started_index_attempts
from danswer.db.index_attempt import mark_attempt_failed
from danswer.db.models import Connector
from danswer.db.models import EmbeddingModel
from danswer.db.models import IndexAttempt
from danswer.db.models import IndexingStatus
from danswer.db.models import IndexModelStatus
from danswer.db.swap_index import check_index_swap
from danswer.search.search_nlp_models import warm_up_encoders
from danswer.utils.logger import setup_logger
from danswer.utils.variable_functionality import global_version
from danswer.utils.variable_functionality import set_is_ee_based_on_env_variable
from shared_configs.configs import INDEXING_MODEL_SERVER_HOST
from shared_configs.configs import LOG_LEVEL
from shared_configs.configs import MODEL_SERVER_PORT

logger = setup_logger()

# If the indexing dies, it's most likely due to resource constraints,
# restarting just delays the eventual failure, not useful to the user
dask.config.set({"distributed.scheduler.allowed-failures": 0})

_UNEXPECTED_STATE_FAILURE_REASON = (
    "Stopped mid run, likely due to the background process being killed"
)


def _should_create_new_indexing(
    connector: Connector,
    last_index: IndexAttempt | None,
    model: EmbeddingModel,
    secondary_index_building: bool,
    db_session: Session,
) -> bool:
    # User can still manually create single indexing attempts via the UI for the
    # currently in use index
    if DISABLE_INDEX_UPDATE_ON_SWAP:
        if model.status == IndexModelStatus.PRESENT and secondary_index_building:
            return False

    # When switching over models, always index at least once
    if model.status == IndexModelStatus.FUTURE and not last_index:
        if connector.id == 0:  # Ingestion API
            return False
        return True

    # If the connector is disabled, don't index
    # NOTE: during an embedding model switch over, we ignore this
    # and index the disabled connectors as well (which is why this if
    # statement is below the first condition above)
    if connector.disabled:
        return False

    if connector.refresh_freq is None:
        return False
    if not last_index:
        return True

    # Only one scheduled job per connector at a time
    # Can schedule another one if the current one is already running however
    # Because the currently running one will not be until the latest time
    # Note, this last index is for the given embedding model
    if last_index.status == IndexingStatus.NOT_STARTED:
        return False

    current_db_time = get_db_current_time(db_session)
    time_since_index = current_db_time - last_index.time_updated
    return time_since_index.total_seconds() >= connector.refresh_freq


def _is_indexing_job_marked_as_finished(index_attempt: IndexAttempt | None) -> bool:
    if index_attempt is None:
        return False

    return (
        index_attempt.status == IndexingStatus.FAILED
        or index_attempt.status == IndexingStatus.SUCCESS
    )


def _mark_run_failed(
    db_session: Session, index_attempt: IndexAttempt, failure_reason: str
) -> None:
    """Marks the `index_attempt` row as failed + updates the `
    connector_credential_pair` to reflect that the run failed"""
    logger.warning(
        f"Marking in-progress attempt 'connector: {index_attempt.connector_id}, "
        f"credential: {index_attempt.credential_id}' as failed due to {failure_reason}"
    )
    mark_attempt_failed(
        index_attempt=index_attempt,
        db_session=db_session,
        failure_reason=failure_reason,
    )


"""Main funcs"""


def create_indexing_jobs(existing_jobs: dict[int, Future | SimpleJob]) -> None:
    """Creates new indexing jobs for each connector / credential pair which is:
    1. Enabled
    2. `refresh_frequency` time has passed since the last indexing run for this pair
    3. There is not already an ongoing indexing attempt for this pair
    """
    with Session(get_sqlalchemy_engine()) as db_session:
        ongoing: set[tuple[int | None, int | None, int]] = set()
        for attempt_id in existing_jobs:
            attempt = get_index_attempt(
                db_session=db_session, index_attempt_id=attempt_id
            )
            if attempt is None:
                logger.error(
                    f"Unable to find IndexAttempt for ID '{attempt_id}' when creating "
                    "indexing jobs"
                )
                continue
            ongoing.add(
                (
                    attempt.connector_id,
                    attempt.credential_id,
                    attempt.embedding_model_id,
                )
            )

        embedding_models = [get_current_db_embedding_model(db_session)]
        secondary_embedding_model = get_secondary_db_embedding_model(db_session)
        if secondary_embedding_model is not None:
            embedding_models.append(secondary_embedding_model)

        all_connectors = fetch_connectors(db_session)
        for connector in all_connectors:
            for association in connector.credentials:
                for model in embedding_models:
                    credential = association.credential

                    # Check if there is an ongoing indexing attempt for this connector + credential pair
                    if (connector.id, credential.id, model.id) in ongoing:
                        continue

                    last_attempt = get_last_attempt(
                        connector.id, credential.id, model.id, db_session
                    )
                    if not _should_create_new_indexing(
                        connector=connector,
                        last_index=last_attempt,
                        model=model,
                        secondary_index_building=len(embedding_models) > 1,
                        db_session=db_session,
                    ):
                        continue

                    create_index_attempt(
                        connector.id, credential.id, model.id, db_session
                    )


def cleanup_indexing_jobs(
    existing_jobs: dict[int, Future | SimpleJob],
    timeout_hours: int = CLEANUP_INDEXING_JOBS_TIMEOUT,
) -> dict[int, Future | SimpleJob]:
    existing_jobs_copy = existing_jobs.copy()

    # clean up completed jobs
    with Session(get_sqlalchemy_engine()) as db_session:
        for attempt_id, job in existing_jobs.items():
            index_attempt = get_index_attempt(
                db_session=db_session, index_attempt_id=attempt_id
            )

            # do nothing for ongoing jobs that haven't been stopped
            if not job.done() and not _is_indexing_job_marked_as_finished(
                index_attempt
            ):
                continue

            if job.status == "error":
                logger.error(job.exception())

            job.release()
            del existing_jobs_copy[attempt_id]

            if not index_attempt:
                logger.error(
                    f"Unable to find IndexAttempt for ID '{attempt_id}' when cleaning "
                    "up indexing jobs"
                )
                continue

            if (
                index_attempt.status == IndexingStatus.IN_PROGRESS
                or job.status == "error"
            ):
                _mark_run_failed(
                    db_session=db_session,
                    index_attempt=index_attempt,
                    failure_reason=_UNEXPECTED_STATE_FAILURE_REASON,
                )

        # clean up in-progress jobs that were never completed
        connectors = fetch_connectors(db_session)
        for connector in connectors:
            in_progress_indexing_attempts = get_inprogress_index_attempts(
                connector.id, db_session
            )
            for index_attempt in in_progress_indexing_attempts:
                if index_attempt.id in existing_jobs:
                    # If index attempt is canceled, stop the run
                    if index_attempt.status == IndexingStatus.FAILED:
                        existing_jobs[index_attempt.id].cancel()
                    # check to see if the job has been updated in last `timeout_hours` hours, if not
                    # assume it to frozen in some bad state and just mark it as failed. Note: this relies
                    # on the fact that the `time_updated` field is constantly updated every
                    # batch of documents indexed
                    current_db_time = get_db_current_time(db_session=db_session)
                    time_since_update = current_db_time - index_attempt.time_updated
                    if time_since_update.total_seconds() > 60 * 60 * timeout_hours:
                        existing_jobs[index_attempt.id].cancel()
                        _mark_run_failed(
                            db_session=db_session,
                            index_attempt=index_attempt,
                            failure_reason="Indexing run frozen - no updates in the last three hours. "
                            "The run will be re-attempted at next scheduled indexing time.",
                        )
                else:
                    # If job isn't known, simply mark it as failed
                    _mark_run_failed(
                        db_session=db_session,
                        index_attempt=index_attempt,
                        failure_reason=_UNEXPECTED_STATE_FAILURE_REASON,
                    )

    return existing_jobs_copy


_DISPATCH = "dispatch"
_DEFER_CC_PAIR = "defer_cc_pair"
_DEFER_SOURCE_CAP = "defer_source_cap"


def _build_running_view(
    in_progress_rows: list[IndexAttempt],
    dispatched_pre_completion_rows: list[IndexAttempt],
    per_source_cap: int,
    cap_overrides: dict[str, int] | None = None,
) -> tuple[dict[str, int], set[tuple[int | None, int | None, int]]]:
    """Build the scheduler's running-attempt view.

    Counts BOTH:
      - Attempts that have flipped to IN_PROGRESS in the DB.
      - Attempts that the scheduler has already dispatched to Dask but
        which haven't yet flipped to IN_PROGRESS (still queued in Dask
        or with a worker spinning up).

    Counting only the DB view leaks the per-source cap: an attempt
    sitting in Dask's queue for a few seconds doesn't register, and
    the next scheduler tick can submit a second same-source attempt
    past the cap. That was the actual cause of "two slack runs at
    once despite cap=1" + "lower-priority attempt running while a
    higher-priority attempt sits NOT_STARTED".
    """
    overrides = cap_overrides or {}
    running_per_source: dict[str, int] = {}
    in_progress_cc_pair_keys: set[tuple[int | None, int | None, int]] = set()
    accounted: set[int] = set()
    for ip in in_progress_rows:
        if ip.id in accounted:
            continue
        accounted.add(ip.id)
        if ip.connector is None:
            continue
        in_progress_cc_pair_keys.add(
            (ip.connector_id, ip.credential_id, ip.embedding_model_id)
        )
        key = ip.connector.source.value
        if cap_for_source(key, per_source_cap, overrides) > 0:
            running_per_source[key] = running_per_source.get(key, 0) + 1
    for d in dispatched_pre_completion_rows:
        if d.id in accounted:
            continue
        accounted.add(d.id)
        if d.connector is None:
            continue
        in_progress_cc_pair_keys.add(
            (d.connector_id, d.credential_id, d.embedding_model_id)
        )
        key = d.connector.source.value
        if cap_for_source(key, per_source_cap, overrides) > 0:
            running_per_source[key] = running_per_source.get(key, 0) + 1
    return running_per_source, in_progress_cc_pair_keys


def _evaluate_dispatch_for_attempt(
    attempt: IndexAttempt,
    running_per_source: dict[str, int],
    in_progress_cc_pair_keys: set[tuple[int | None, int | None, int]],
    per_source_cap: int,
    cap_overrides: dict[str, int] | None = None,
) -> str:
    """Pure decision: should this attempt dispatch now, or defer?

    Mutates `running_per_source` and `in_progress_cc_pair_keys` only
    when returning _DISPATCH, so subsequent same-tick iterations see
    this attempt as in-flight.
    """
    if attempt.connector is None:
        return _DISPATCH  # caller handles connector-null path separately
    cc_pair_key = (
        attempt.connector_id,
        attempt.credential_id,
        attempt.embedding_model_id,
    )
    if cc_pair_key in in_progress_cc_pair_keys:
        return _DEFER_CC_PAIR
    source_key = attempt.connector.source.value
    source_cap = cap_for_source(source_key, per_source_cap, cap_overrides or {})
    if source_cap > 0:
        if running_per_source.get(source_key, 0) >= source_cap:
            return _DEFER_SOURCE_CAP
        running_per_source[source_key] = running_per_source.get(source_key, 0) + 1
    in_progress_cc_pair_keys.add(cc_pair_key)
    return _DISPATCH


def kickoff_indexing_jobs(
    existing_jobs: dict[int, Future | SimpleJob],
    client: Client | SimpleJobClient,
    secondary_client: Client | SimpleJobClient,
) -> dict[int, Future | SimpleJob]:
    existing_jobs_copy = existing_jobs.copy()
    engine = get_sqlalchemy_engine()

    # Don't include jobs waiting in the Dask queue that just haven't started running
    # Also (rarely) don't include for jobs that started but haven't updated the indexing tables yet
    with Session(engine) as db_session:
        new_indexing_attempts = [
            (attempt, attempt.embedding_model)
            for attempt in get_not_started_index_attempts(db_session)
            if attempt.id not in existing_jobs
        ]

        # One DB read fuels two scheduler-side guards:
        #   (a) per-source-type concurrency cap — defer when the source
        #       group is full (`PER_SOURCE_CAP`).
        #   (b) per-cc-pair collision guard — defer when this exact
        #       (connector, credential, embedding_model) tuple already
        #       has an IN_PROGRESS attempt. Common case: a manual
        #       Re-Index click while an auto-scheduled run is mid-flight.
        # Both deferrals leave the row as NOT_STARTED — never FAILED —
        # so the indexing-status table doesn't get polluted with
        # "skipped_concurrent_*" rows for routine queueing decisions.
        in_progress_rows = (
            db_session.query(IndexAttempt)
            .filter(IndexAttempt.status == IndexingStatus.IN_PROGRESS)
            .all()
        )
        accounted_attempt_ids = {ip.id for ip in in_progress_rows}
        unaccounted_dispatched_ids = [
            aid for aid in existing_jobs if aid not in accounted_attempt_ids
        ]
        dispatched_pre_completion_rows: list[IndexAttempt] = []
        if unaccounted_dispatched_ids:
            dispatched_pre_completion_rows = (
                db_session.query(IndexAttempt)
                .filter(IndexAttempt.id.in_(unaccounted_dispatched_ids))
                .filter(
                    IndexAttempt.status.notin_(
                        [IndexingStatus.SUCCESS, IndexingStatus.FAILED]
                    )
                )
                .all()
            )
        running_per_source, in_progress_cc_pair_keys = _build_running_view(
            in_progress_rows,
            dispatched_pre_completion_rows,
            PER_SOURCE_CAP,
            PER_SOURCE_CAP_OVERRIDES,
        )

    logger.info(f"Found {len(new_indexing_attempts)} new indexing tasks.")

    if not new_indexing_attempts:
        return existing_jobs

    for attempt, embedding_model in new_indexing_attempts:
        use_secondary_index = (
            embedding_model.status == IndexModelStatus.FUTURE
            if embedding_model is not None
            else False
        )
        if attempt.connector is None:
            logger.warning(
                f"Skipping index attempt as Connector has been deleted: {attempt}"
            )
            with Session(engine) as db_session:
                mark_attempt_failed(
                    attempt, db_session, failure_reason="Connector is null"
                )
            continue
        if attempt.credential is None:
            logger.warning(
                f"Skipping index attempt as Credential has been deleted: {attempt}"
            )
            with Session(engine) as db_session:
                mark_attempt_failed(
                    attempt, db_session, failure_reason="Credential is null"
                )
            continue

        decision = _evaluate_dispatch_for_attempt(
            attempt,
            running_per_source,
            in_progress_cc_pair_keys,
            PER_SOURCE_CAP,
            PER_SOURCE_CAP_OVERRIDES,
        )
        if decision == _DEFER_CC_PAIR:
            logger.info(
                f"Deferring indexing attempt {attempt.id} for connector "
                f"'{attempt.connector.name}': cc-pair already has an "
                "IN_PROGRESS attempt. Will retry on next scheduler tick."
            )
            continue
        if decision == _DEFER_SOURCE_CAP:
            logger.info(
                f"Deferring indexing attempt {attempt.id} for connector "
                f"'{attempt.connector.name}' "
                f"(source={attempt.connector.source.value}): "
                f"cap of "
                f"{cap_for_source(attempt.connector.source.value, PER_SOURCE_CAP, PER_SOURCE_CAP_OVERRIDES)}"
                f" reached. "
                "Will retry on next scheduler tick."
            )
            continue

        # Per-attempt indexing priority. SimpleJobClient ignores the kwarg;
        # the real Dask Client honors it (higher number = scheduled first).
        priority = int(attempt.indexing_priority or 0)
        submit_kwargs: dict[str, Any] = {"pure": False}
        if isinstance(client, Client):
            submit_kwargs["priority"] = priority

        if use_secondary_index:
            run = secondary_client.submit(
                run_indexing_entrypoint,
                attempt.id,
                global_version.get_is_ee_version(),
                **submit_kwargs,
            )
        else:
            run = client.submit(
                run_indexing_entrypoint,
                attempt.id,
                global_version.get_is_ee_version(),
                **submit_kwargs,
            )

        if run:
            secondary_str = "(secondary index) " if use_secondary_index else ""
            priority_str = f", priority: {priority}" if priority else ""
            logger.info(
                f"Kicked off {secondary_str}"
                f"indexing attempt for connector: '{attempt.connector.name}', "
                f"with config: '{attempt.connector.connector_specific_config}', and "
                f"with credentials: '{attempt.credential_id}'{priority_str}"
            )
            existing_jobs_copy[attempt.id] = run

    return existing_jobs_copy


def update_loop(delay: int = 10, num_workers: int = NUM_INDEXING_WORKERS) -> None:
    engine = get_sqlalchemy_engine()
    with Session(engine) as db_session:
        check_index_swap(db_session=db_session)
        db_embedding_model = get_current_db_embedding_model(db_session)

    # So that the first time users aren't surprised by really slow speed of first
    # batch of documents indexed
    logger.info("Running a first inference to warm up embedding model")
    warm_up_encoders(
        model_name=db_embedding_model.model_name,
        normalize=db_embedding_model.normalize,
        model_server_host=INDEXING_MODEL_SERVER_HOST,
        model_server_port=MODEL_SERVER_PORT,
    )

    # Pick the indexing-execution backend in priority order:
    #
    # 1. DASK_SCHEDULER_ADDRESS — production mode in K8s. Indexing
    #    work is dispatched to a remote Dask scheduler service that
    #    fans out to a horizontally-scalable pool of `dask-worker`
    #    pods. This is the only mode that supports scaling indexing
    #    concurrency by adding pods (vs the in-pod LocalCluster which
    #    is bounded by the host's RAM).
    # 2. DASK_JOB_CLIENT_ENABLED — legacy in-process Dask LocalCluster.
    #    All workers in the same Python process. Used in dev and in
    #    pre-distributed-mode prod deployments.
    # 3. SimpleJobClient — bare ProcessPoolExecutor-style fallback.
    #    Used by some local dev flows that don't want the Dask
    #    overhead.
    #
    # The remote-scheduler path uses two named queues — `primary` for
    # the active embedding model and `secondary` for the in-flight
    # secondary index during model swaps — so a single dask-scheduler
    # service serves both without code changes elsewhere.
    client_primary: Client | SimpleJobClient
    client_secondary: Client | SimpleJobClient
    dask_scheduler_address = os.environ.get("DASK_SCHEDULER_ADDRESS")
    if dask_scheduler_address:
        logger.info("Connecting to remote Dask scheduler at %s", dask_scheduler_address)
        client_primary = Client(dask_scheduler_address)
        client_secondary = Client(dask_scheduler_address)
        if LOG_LEVEL.lower() == "debug":
            client_primary.register_worker_plugin(ResourceLogger())
    elif DASK_JOB_CLIENT_ENABLED:
        cluster_primary = LocalCluster(
            n_workers=num_workers,
            threads_per_worker=1,
            # there are warning about high memory usage + "Event loop unresponsive"
            # which are not relevant to us since our workers are expected to use a
            # lot of memory + involve CPU intensive tasks that will not relinquish
            # the event loop
            silence_logs=logging.ERROR,
        )
        cluster_secondary = LocalCluster(
            n_workers=num_workers,
            threads_per_worker=1,
            silence_logs=logging.ERROR,
        )
        client_primary = Client(cluster_primary)
        client_secondary = Client(cluster_secondary)
        if LOG_LEVEL.lower() == "debug":
            client_primary.register_worker_plugin(ResourceLogger())
    else:
        client_primary = SimpleJobClient(n_workers=num_workers)
        client_secondary = SimpleJobClient(n_workers=num_workers)

    existing_jobs: dict[int, Future | SimpleJob] = {}

    while True:
        start = time.time()
        start_time_utc = datetime.utcfromtimestamp(start).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"Running update, current UTC time: {start_time_utc}")

        if existing_jobs:
            # TODO: make this debug level once the "no jobs are being scheduled" issue is resolved
            logger.info(
                "Found existing indexing jobs: "
                f"{[(attempt_id, job.status) for attempt_id, job in existing_jobs.items()]}"
            )

        try:
            with Session(get_sqlalchemy_engine()) as db_session:
                check_index_swap(db_session)
            existing_jobs = cleanup_indexing_jobs(existing_jobs=existing_jobs)
            create_indexing_jobs(existing_jobs=existing_jobs)
            existing_jobs = kickoff_indexing_jobs(
                existing_jobs=existing_jobs,
                client=client_primary,
                secondary_client=client_secondary,
            )
        except Exception as e:
            logger.exception(f"Failed to run update due to {e}")
        sleep_time = delay - (time.time() - start)
        if sleep_time > 0:
            time.sleep(sleep_time)


def update__main() -> None:
    set_is_ee_based_on_env_variable()

    logger.info("Starting Indexing Loop")
    update_loop()


if __name__ == "__main__":
    update__main()
