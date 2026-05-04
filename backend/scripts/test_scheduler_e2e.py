"""End-to-end integration tests for ``kickoff_indexing_jobs``.

Companion to ``test_features_e2e.py`` and ``test_analytics_e2e.py``. This
script seeds real ``connector`` / ``credential`` / ``connector_credential_pair``
/ ``index_attempt`` rows in the configured Postgres, then drives the real
``kickoff_indexing_jobs`` function with a recording fake Dask client. The
fake records every ``submit`` call without spawning a worker, so we can
assert on the scheduler's dispatch decisions against live SQLAlchemy ORM,
the live priority sort in ``get_not_started_index_attempts``, and the
live cap / cc-pair guard logic.

The unit tests in ``tests/unit/danswer/background/test_indexing_scheduler.py``
exercise the pure helpers in isolation. This script proves the same
guarantees hold when the helpers are wired through the ORM, ``Session``,
``IndexAttempt`` filters, and the ``existing_jobs`` ↔ DB-IN_PROGRESS
accounting that fixed the cap-leak bug.

Phases:
  Phase A — priority order under cap=1: highest-priority slack attempt
            wins; lower-priority same-source attempts stay NOT_STARTED.
  Phase B — cap-leak regression: a slack attempt sitting in
            ``existing_jobs`` (Dask-queued, DB row still NOT_STARTED)
            must hold its source-cap slot. A second slack candidate in
            the same tick must NOT be dispatched. Pre-fix, it leaked.
  Phase C — per-cc-pair guard with a real IN_PROGRESS row: a manual
            Re-Index colliding with an auto-scheduled run must be
            deferred (no FAILED row, just NOT_STARTED waiting).
  Phase D — different sources don't share the cap: cap=1 + slack +
            github + confluence all NOT_STARTED → all three dispatched
            in one tick.
  Phase E — same-tick same-cc-pair double-submit prevented: two
            NOT_STARTED rows for the same cc-pair → only one dispatches.
  Phase F — soak: run the same scheduler tick N times against a
            randomized seed of attempts, asserting cap + cc-pair
            invariants every iteration. This is the integration twin
            of the unit-level fuzz test, but driving the real DB.

DESTRUCTIVE: writes/deletes tagged data only (``__test_scheduler__``
prefix). Run only against a dev / staging DB.

Pre-flight: refuses to run if the production indexer is up — it would
race on our seeded NOT_STARTED rows. Stop it with::

    pkill -f 'danswer/background/update.py'

…or run with ``--force-with-indexer-running`` to override (your test
attempts may then be picked up by the real scheduler before this
script asserts on them — expect noise).

Usage::

    cd backend
    PYTHONPATH=$(pwd) python scripts/test_scheduler_e2e.py [--yes] [--keep-data] \\
        [--soak-iterations=20] [--force-with-indexer-running]
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import random
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.orm import Session

import danswer.background.update as scheduler
from danswer.configs.constants import DocumentSource
from danswer.connectors.models import InputType
from danswer.db.engine import get_sqlalchemy_engine
from danswer.db.models import Connector
from danswer.db.models import ConnectorCredentialPair
from danswer.db.models import Credential
from danswer.db.models import EmbeddingModel
from danswer.db.models import IndexAttempt
from danswer.db.models import IndexingStatus


SCHEDULER_PREFIX = "__test_scheduler__"


# Sources we use for test connectors — chosen from DocumentSource members
# that are *unlikely* to be used in any real environment, so production
# NOT_STARTED rows can't leak into the cap accounting alongside ours.
# `kickoff_indexing_jobs` legitimately considers all NOT_STARTED rows in
# the DB, not just tagged ones; if the test used SLACK/GITHUB and the
# environment had production NOT_STARTED rows on those sources, the cap
# would already be claimed by them and the test wouldn't isolate
# scheduler behaviour from environment state.
_SAFE_SOURCES_PRIMARY = [
    DocumentSource.GMAIL,
    DocumentSource.ZULIP,
    DocumentSource.JIRA,
    DocumentSource.ZENDESK,
    DocumentSource.NOTION,
    DocumentSource.LINEAR,
]


def pick_safe_sources(db: Session, n: int) -> list[DocumentSource]:
    """Return ``n`` distinct DocumentSource values that have NO existing
    rows in either ``connector`` or NOT_STARTED ``index_attempt`` (other
    than tagged test rows). If insufficient unused sources are
    available, abort with a clear message — the test framework can't
    isolate scheduler behaviour from environment otherwise."""
    used: set[str] = set()
    rows = db.execute(
        text("SELECT DISTINCT source FROM connector WHERE name NOT LIKE :p"),
        {"p": f"{SCHEDULER_PREFIX}%"},
    ).fetchall()
    used.update(r[0].upper() for r in rows)
    rows = db.execute(
        text(
            "SELECT DISTINCT co.source FROM index_attempt ia "
            "JOIN connector co ON co.id = ia.connector_id "
            "WHERE ia.status = 'NOT_STARTED' AND co.name NOT LIKE :p"
        ),
        {"p": f"{SCHEDULER_PREFIX}%"},
    ).fetchall()
    used.update(r[0].upper() for r in rows)

    safe = [s for s in _SAFE_SOURCES_PRIMARY if s.name.upper() not in used]
    if len(safe) < n:
        sys.exit(
            f"Need {n} unused DocumentSource values for the test; "
            f"only {len(safe)} are free in this DB. Used by environment: "
            f"{sorted(used)}. Free: {[s.name for s in safe]}. Either "
            "delete the conflicting connectors or extend "
            "_SAFE_SOURCES_PRIMARY."
        )
    return safe[:n]


# ---------------------------------------------------------------------------
# Tiny harness (mirrors the other e2e scripts)
# ---------------------------------------------------------------------------


_FAILED = 0


def section(name: str) -> None:
    print(f"\n=== {name} ===")


def passed(msg: str) -> None:
    print(f"  ok  {msg}")


def failed(msg: str, detail: str | None = None) -> None:
    global _FAILED
    _FAILED += 1
    print(f"  XX  {msg}")
    if detail:
        for line in detail.splitlines():
            print(f"      {line}")


def assert_eq(actual: Any, expected: Any, label: str) -> None:
    if actual == expected:
        passed(f"{label} — {actual}")
    else:
        failed(label, f"expected={expected}, actual={actual}")


def assert_set_eq(actual: set, expected: set, label: str) -> None:
    if actual == expected:
        passed(f"{label} — {sorted(actual)}")
    else:
        failed(
            label,
            f"expected={sorted(expected)}, actual={sorted(actual)}, "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}",
        )


def assert_true(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        passed(label)
    else:
        failed(label, detail or None)


# ---------------------------------------------------------------------------
# Recording fake Dask client
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _RecordedJob:
    """Stand-in for ``dask.distributed.Future`` / ``SimpleJob``. The
    scheduler stores it in ``existing_jobs`` and queries ``done()`` on
    the next cleanup pass. We pretend the job is still running."""

    job_id: int

    def done(self) -> bool:
        return False

    def cancel(self) -> None:  # pragma: no cover — not exercised here
        pass

    def release(self) -> None:  # pragma: no cover
        pass

    @property
    def status(self) -> str:
        return "pending"

    def exception(self) -> str:  # pragma: no cover
        return ""


@dataclasses.dataclass
class _RecordedSubmit:
    func: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


class RecordingClient:
    """Replacement for ``dask.distributed.Client`` that records every
    ``submit`` call without spawning a worker. Crucially this is *not*
    a subclass of ``dask.distributed.Client``, so the scheduler's
    ``isinstance(client, Client)`` check is False and it won't pass the
    ``priority`` kwarg. Priority kwarg behaviour is covered by the
    unit-test ``test_priority_strict_order_under_cap_one``; here we
    care about the dispatch / defer decisions."""

    def __init__(self) -> None:
        self.submitted: list[_RecordedSubmit] = []
        self._counter = 0

    def submit(
        self, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> _RecordedJob:
        self._counter += 1
        self.submitted.append(_RecordedSubmit(func=func, args=args, kwargs=kwargs))
        return _RecordedJob(job_id=self._counter)

    def submitted_attempt_ids(self) -> set[int]:
        # `run_indexing_entrypoint` is called as
        # `submit(run_indexing_entrypoint, attempt.id, is_ee_version)`.
        return {int(rec.args[0]) for rec in self.submitted if rec.args}


# ---------------------------------------------------------------------------
# Seed helpers (everything tagged with SCHEDULER_PREFIX)
# ---------------------------------------------------------------------------


def lookup_embedding_model(db: Session) -> EmbeddingModel:
    em = db.execute(
        select(EmbeddingModel).order_by(EmbeddingModel.id).limit(1)
    ).scalar_one_or_none()
    if em is None:
        sys.exit(
            "No embedding_model found. Bootstrap your DB first "
            "(alembic upgrade head + start the API server once)."
        )
    return em


def make_connector_and_pair(
    db: Session, source: DocumentSource
) -> tuple[Connector, Credential, ConnectorCredentialPair]:
    """Create a connector + credential + cc-pair tagged with the test
    prefix. The connector is ``disabled=True`` so the production
    scheduler's `_should_create_new_indexing` won't auto-spawn fresh
    attempts mid-test (we manually seed the attempts we need)."""
    connector = Connector(
        name=f"{SCHEDULER_PREFIX}{source.value}-{uuid.uuid4().hex[:6]}",
        source=source,
        input_type=InputType.POLL,
        connector_specific_config={"_test_scheduler": True},
        refresh_freq=600,
        disabled=True,  # belt-and-suspenders against the prod scheduler
    )
    credential = Credential(admin_public=True, credential_json={})
    db.add_all([connector, credential])
    db.flush()
    ccp = ConnectorCredentialPair(
        connector_id=connector.id,
        credential_id=credential.id,
        name=f"{SCHEDULER_PREFIX}ccp-{uuid.uuid4().hex[:6]}",
        is_public=True,
        total_docs_indexed=0,
    )
    db.add(ccp)
    db.flush()
    return connector, credential, ccp


def make_attempt(
    db: Session,
    *,
    connector_id: int,
    credential_id: int,
    embedding_model_id: int,
    status: IndexingStatus,
    priority: int = 0,
    seconds_old: int = 0,
) -> IndexAttempt:
    when = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(
        seconds=seconds_old
    )
    attempt = IndexAttempt(
        connector_id=connector_id,
        credential_id=credential_id,
        embedding_model_id=embedding_model_id,
        from_beginning=False,
        status=status,
        new_docs_indexed=0,
        total_docs_indexed=0,
        docs_removed_from_index=0,
        indexing_priority=priority,
        time_created=when,
        time_updated=when,
        time_started=when if status != IndexingStatus.NOT_STARTED else None,
    )
    db.add(attempt)
    db.flush()
    return attempt


# ---------------------------------------------------------------------------
# Tick driver
# ---------------------------------------------------------------------------


def run_one_tick(
    *,
    cap: int,
    existing_jobs: dict[int, _RecordedJob] | None = None,
) -> tuple[RecordingClient, dict[int, _RecordedJob]]:
    """Drive ``kickoff_indexing_jobs`` exactly once with a fresh
    recording client and the requested ``existing_jobs`` map."""
    primary = RecordingClient()
    secondary = RecordingClient()
    prev_cap = scheduler.PER_SOURCE_CAP
    scheduler.PER_SOURCE_CAP = cap
    try:
        next_jobs = scheduler.kickoff_indexing_jobs(
            existing_jobs or {},
            primary,  # type: ignore[arg-type]
            secondary,  # type: ignore[arg-type]
        )
    finally:
        scheduler.PER_SOURCE_CAP = prev_cap
    return primary, next_jobs


def fetch_attempt(db: Session, attempt_id: int) -> IndexAttempt:
    attempt = db.execute(
        select(IndexAttempt).where(IndexAttempt.id == attempt_id)
    ).scalar_one()
    return attempt


# ---------------------------------------------------------------------------
# Phase A — priority order under cap=1
# ---------------------------------------------------------------------------


def phase_a_priority_under_cap() -> None:
    section("Phase A — priority order under cap=1 (mirrors user scenario)")

    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        em = lookup_embedding_model(db)
        (test_source,) = pick_safe_sources(db, 1)
        # Three same-source cc-pairs, all NOT_STARTED. The middle one is
        # priority-bumped to 20; the others sit at 0. This is the exact
        # shape the user reported (3 Slack connectors, one bumped) but
        # on a source the environment isn't using.
        attempt_ids: list[int] = []
        priority_for: dict[int, int] = {}
        for i, prio in enumerate([0, 20, 0]):
            _, _, ccp = make_connector_and_pair(db, test_source)
            a = make_attempt(
                db,
                connector_id=ccp.connector_id,
                credential_id=ccp.credential_id,
                embedding_model_id=em.id,
                status=IndexingStatus.NOT_STARTED,
                priority=prio,
                seconds_old=(3 - i) * 30,  # FIFO tiebreak: deterministic
            )
            attempt_ids.append(a.id)
            priority_for[a.id] = prio
        db.commit()

    bumped_id = next(aid for aid, p in priority_for.items() if p == 20)
    primary, _ = run_one_tick(cap=1)
    submitted = primary.submitted_attempt_ids() & set(attempt_ids)
    assert_set_eq(
        submitted,
        {bumped_id},
        "cap=1: only the priority-20 attempt is dispatched",
    )

    # Verify the other two are still NOT_STARTED in DB (deferred, not
    # marked failed).
    with Session(engine) as db:
        for aid in attempt_ids:
            if aid == bumped_id:
                continue
            a = fetch_attempt(db, aid)
            assert_eq(
                a.status,
                IndexingStatus.NOT_STARTED,
                f"deferred attempt {aid} stays NOT_STARTED (no FAILED row)",
            )


# ---------------------------------------------------------------------------
# Phase B — cap-leak regression (THE bug fix)
# ---------------------------------------------------------------------------


def phase_b_cap_leak_regression() -> None:
    section("Phase B — cap-leak: dispatched attempt holds its source slot")

    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        em = lookup_embedding_model(db)
        (test_source,) = pick_safe_sources(db, 1)
        _, _, ccp_1 = make_connector_and_pair(db, test_source)
        _, _, ccp_2 = make_connector_and_pair(db, test_source)
        # Both NOT_STARTED. ``in_flight`` is the one we'll pretend was
        # already dispatched in a prior tick (it sits in existing_jobs
        # with the DB row still NOT_STARTED — the exact window where
        # the pre-fix scheduler leaked the cap).
        in_flight = make_attempt(
            db,
            connector_id=ccp_1.connector_id,
            credential_id=ccp_1.credential_id,
            embedding_model_id=em.id,
            status=IndexingStatus.NOT_STARTED,
            priority=0,
            seconds_old=120,
        )
        candidate = make_attempt(
            db,
            connector_id=ccp_2.connector_id,
            credential_id=ccp_2.credential_id,
            embedding_model_id=em.id,
            status=IndexingStatus.NOT_STARTED,
            priority=0,
            seconds_old=60,
        )
        db.commit()
        in_flight_id = in_flight.id
        candidate_id = candidate.id

    # Simulate "in_flight was dispatched in the previous tick but the
    # worker hasn't flipped the row to IN_PROGRESS yet".
    existing_jobs: dict[int, _RecordedJob] = {in_flight_id: _RecordedJob(job_id=999)}
    primary, _ = run_one_tick(cap=1, existing_jobs=existing_jobs)
    submitted = primary.submitted_attempt_ids() & {in_flight_id, candidate_id}

    # Pre-fix expectation (BUG): submitted == {candidate_id} (leaked).
    # Post-fix expectation: submitted == set() (cap held by in_flight).
    assert_set_eq(
        submitted,
        set(),
        "cap-leak fix: dispatched-but-not-IN_PROGRESS attempt blocks "
        "second same-source candidate",
    )

    with Session(engine) as db:
        a = fetch_attempt(db, candidate_id)
        assert_eq(
            a.status,
            IndexingStatus.NOT_STARTED,
            f"candidate {candidate_id} stays NOT_STARTED (no FAILED)",
        )


# ---------------------------------------------------------------------------
# Phase C — per-cc-pair guard with a real IN_PROGRESS row
# ---------------------------------------------------------------------------


def phase_c_cc_pair_guard_with_in_progress() -> None:
    section("Phase C — per-cc-pair guard: Re-Index collides with auto-run")

    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        em = lookup_embedding_model(db)
        (test_source,) = pick_safe_sources(db, 1)
        _, _, ccp = make_connector_and_pair(db, test_source)
        running = make_attempt(
            db,
            connector_id=ccp.connector_id,
            credential_id=ccp.credential_id,
            embedding_model_id=em.id,
            status=IndexingStatus.IN_PROGRESS,
            seconds_old=60,
        )
        reindex = make_attempt(
            db,
            connector_id=ccp.connector_id,
            credential_id=ccp.credential_id,
            embedding_model_id=em.id,
            status=IndexingStatus.NOT_STARTED,
        )
        db.commit()
        running_id = running.id
        reindex_id = reindex.id

    primary, _ = run_one_tick(cap=4)  # cap large enough that source isn't the limit
    submitted = primary.submitted_attempt_ids() & {running_id, reindex_id}
    assert_set_eq(
        submitted,
        set(),
        "manual Re-Index attempt deferred while IN_PROGRESS exists for cc-pair",
    )

    with Session(engine) as db:
        a = fetch_attempt(db, reindex_id)
        assert_eq(
            a.status,
            IndexingStatus.NOT_STARTED,
            "Re-Index stays NOT_STARTED (per-cc-pair guard, no FAILED row)",
        )


# ---------------------------------------------------------------------------
# Phase D — different sources don't share the cap
# ---------------------------------------------------------------------------


def phase_d_different_sources_independent_caps() -> None:
    section("Phase D — different sources have independent caps")

    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        em = lookup_embedding_model(db)
        test_sources = pick_safe_sources(db, 3)
        attempt_ids: list[int] = []
        for src in test_sources:
            _, _, ccp = make_connector_and_pair(db, src)
            a = make_attempt(
                db,
                connector_id=ccp.connector_id,
                credential_id=ccp.credential_id,
                embedding_model_id=em.id,
                status=IndexingStatus.NOT_STARTED,
            )
            attempt_ids.append(a.id)
        db.commit()

    primary, _ = run_one_tick(cap=1)
    submitted = primary.submitted_attempt_ids() & set(attempt_ids)
    assert_set_eq(
        submitted,
        set(attempt_ids),
        "cap=1 + 3 different sources → all 3 dispatched",
    )


# ---------------------------------------------------------------------------
# Phase E — same-tick same-cc-pair double-submit prevented
# ---------------------------------------------------------------------------


def phase_e_same_cc_pair_double_submit_prevented() -> None:
    section("Phase E — two NOT_STARTED for same cc-pair in one tick")

    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        em = lookup_embedding_model(db)
        (test_source,) = pick_safe_sources(db, 1)
        _, _, ccp = make_connector_and_pair(db, test_source)
        a = make_attempt(
            db,
            connector_id=ccp.connector_id,
            credential_id=ccp.credential_id,
            embedding_model_id=em.id,
            status=IndexingStatus.NOT_STARTED,
            seconds_old=60,
        )
        b = make_attempt(
            db,
            connector_id=ccp.connector_id,
            credential_id=ccp.credential_id,
            embedding_model_id=em.id,
            status=IndexingStatus.NOT_STARTED,
            seconds_old=30,
        )
        db.commit()
        ids = {a.id, b.id}

    primary, _ = run_one_tick(cap=4)
    submitted = primary.submitted_attempt_ids() & ids
    assert_eq(
        len(submitted),
        1,
        "exactly one of the same-cc-pair NOT_STARTED rows dispatched",
    )


# ---------------------------------------------------------------------------
# Phase G — completed existing_jobs entries don't consume cap slots
# ---------------------------------------------------------------------------


def phase_g_completed_in_flight_does_not_block() -> None:
    """The cap-leak fix counts dispatched-but-pre-completion attempts
    toward ``running_per_source``. The flip side: an attempt that has
    *finished* (DB row is SUCCESS or FAILED) must NOT count, even if
    it's still sitting in ``existing_jobs`` (cleanup happens on the
    next scheduler tick). Without this, a one-shot finished job would
    keep its source's cap occupied until cleanup, which would
    artificially serialize the queue.

    The query in ``_build_running_view`` filters with
    ``IndexAttempt.status.notin_([SUCCESS, FAILED])``; this phase
    proves that filter actually excludes a completed row from the
    accounting against a live DB.
    """
    section("Phase G — completed in_flight (SUCCESS) doesn't consume cap")

    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        em = lookup_embedding_model(db)
        (test_source,) = pick_safe_sources(db, 1)
        # `finished_attempt` is in `existing_jobs` (the scheduler hasn't
        # cleaned it yet) but its DB row is already SUCCESS — the
        # accounting must skip it.
        _, _, ccp_done = make_connector_and_pair(db, test_source)
        finished_attempt = make_attempt(
            db,
            connector_id=ccp_done.connector_id,
            credential_id=ccp_done.credential_id,
            embedding_model_id=em.id,
            status=IndexingStatus.SUCCESS,
            priority=0,
            seconds_old=120,
        )
        # Fresh NOT_STARTED of the SAME source; cap=1 must let it
        # through because finished_attempt does NOT hold a slot.
        _, _, ccp_new = make_connector_and_pair(db, test_source)
        candidate = make_attempt(
            db,
            connector_id=ccp_new.connector_id,
            credential_id=ccp_new.credential_id,
            embedding_model_id=em.id,
            status=IndexingStatus.NOT_STARTED,
            priority=0,
            seconds_old=60,
        )
        db.commit()
        finished_id = finished_attempt.id
        candidate_id = candidate.id

    existing_jobs: dict[int, _RecordedJob] = {
        finished_id: _RecordedJob(job_id=finished_id)
    }
    primary, _ = run_one_tick(cap=1, existing_jobs=existing_jobs)
    submitted = primary.submitted_attempt_ids() & {finished_id, candidate_id}
    assert_set_eq(
        submitted,
        {candidate_id},
        "completed in_flight skipped by cap accounting; new candidate dispatches",
    )


# ---------------------------------------------------------------------------
# Phase H — FAILED existing_jobs entries also don't consume cap slots
# ---------------------------------------------------------------------------


def phase_h_failed_in_flight_does_not_block() -> None:
    """Same invariant as Phase G but for the FAILED status — both
    terminal states must be excluded from the dispatched-pre-completion
    accounting."""
    section("Phase H — FAILED in_flight doesn't consume cap")

    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        em = lookup_embedding_model(db)
        (test_source,) = pick_safe_sources(db, 1)
        _, _, ccp_failed = make_connector_and_pair(db, test_source)
        failed_attempt = make_attempt(
            db,
            connector_id=ccp_failed.connector_id,
            credential_id=ccp_failed.credential_id,
            embedding_model_id=em.id,
            status=IndexingStatus.FAILED,
            priority=0,
            seconds_old=120,
        )
        _, _, ccp_new = make_connector_and_pair(db, test_source)
        candidate = make_attempt(
            db,
            connector_id=ccp_new.connector_id,
            credential_id=ccp_new.credential_id,
            embedding_model_id=em.id,
            status=IndexingStatus.NOT_STARTED,
            priority=0,
            seconds_old=60,
        )
        db.commit()
        failed_id = failed_attempt.id
        candidate_id = candidate.id

    existing_jobs: dict[int, _RecordedJob] = {failed_id: _RecordedJob(job_id=failed_id)}
    primary, _ = run_one_tick(cap=1, existing_jobs=existing_jobs)
    submitted = primary.submitted_attempt_ids() & {failed_id, candidate_id}
    assert_set_eq(
        submitted,
        {candidate_id},
        "FAILED in_flight skipped; new candidate dispatches",
    )


# ---------------------------------------------------------------------------
# Phase I — IN_PROGRESS dispatched attempt accounted via DB query, not
# double-counted via existing_jobs path
# ---------------------------------------------------------------------------


def phase_i_in_progress_dispatched_not_double_counted() -> None:
    """When a dispatched attempt has flipped to IN_PROGRESS in the DB,
    the primary IN_PROGRESS query already counts it. The dispatched-
    pre-completion query then runs over `unaccounted_dispatched_ids`,
    skipping anything already in the IN_PROGRESS set. If the dedup
    logic broke, the same attempt would be counted twice and a same-
    source new candidate would be deferred even though only ONE
    cc-pair is actually running.

    With cap=2, an IN_PROGRESS slack + a NOT_STARTED slack candidate
    must both fit (count = 1 + 1 = 2 ≤ cap). If double-counting were
    happening, count would read 2 + 1 = 3 → defer the candidate.
    """
    section("Phase I — IN_PROGRESS dispatched not double-counted (cap=2)")

    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        em = lookup_embedding_model(db)
        (test_source,) = pick_safe_sources(db, 1)
        _, _, ccp_running = make_connector_and_pair(db, test_source)
        running_attempt = make_attempt(
            db,
            connector_id=ccp_running.connector_id,
            credential_id=ccp_running.credential_id,
            embedding_model_id=em.id,
            status=IndexingStatus.IN_PROGRESS,
            priority=0,
            seconds_old=60,
        )
        _, _, ccp_new = make_connector_and_pair(db, test_source)
        candidate = make_attempt(
            db,
            connector_id=ccp_new.connector_id,
            credential_id=ccp_new.credential_id,
            embedding_model_id=em.id,
            status=IndexingStatus.NOT_STARTED,
            priority=0,
            seconds_old=30,
        )
        db.commit()
        running_id = running_attempt.id
        candidate_id = candidate.id

    # `running_attempt` is in BOTH the IN_PROGRESS DB set AND
    # existing_jobs — the de-dup must keep the cap count at 1.
    existing_jobs: dict[int, _RecordedJob] = {
        running_id: _RecordedJob(job_id=running_id)
    }
    primary, _ = run_one_tick(cap=2, existing_jobs=existing_jobs)
    submitted = primary.submitted_attempt_ids() & {running_id, candidate_id}
    assert_set_eq(
        submitted,
        {candidate_id},
        "no double-counting: candidate dispatches under cap=2",
    )


# ---------------------------------------------------------------------------
# Phase F — soak: N randomized ticks, invariants hold every time
# ---------------------------------------------------------------------------


def phase_f_soak_random_ticks(iterations: int) -> None:
    section(f"Phase F — soak: {iterations} randomized ticks")

    rng = random.Random(0xC0DE)
    engine = get_sqlalchemy_engine()
    violations = 0
    with Session(engine) as db:
        sources = pick_safe_sources(db, 4)

    for it in range(iterations):
        # Clean prior iteration's tagged rows so each tick observes a
        # fresh queue. Without this, deferred NOT_STARTED rows from
        # iteration N contaminate iteration N+1's cap accounting.
        cleanup_test_data()
        with Session(engine) as db:
            em = lookup_embedding_model(db)
            n_attempts = rng.randint(3, 8)
            seeded_ids: list[int] = []
            seeded_source_for: dict[int, str] = {}
            seeded_cc_pair_for: dict[int, tuple[int | None, int | None, int]] = {}
            cap = rng.choice([1, 1, 1, 2])  # weighted toward 1

            # Build a small pool of cc-pairs so collisions can happen.
            ccp_pool: list[tuple[Connector, Credential, ConnectorCredentialPair]] = []
            for _ in range(rng.randint(2, 5)):
                src = rng.choice(sources)
                ccp_pool.append(make_connector_and_pair(db, src))

            for _ in range(n_attempts):
                conn, cred, ccp = rng.choice(ccp_pool)
                a = make_attempt(
                    db,
                    connector_id=ccp.connector_id,
                    credential_id=ccp.credential_id,
                    embedding_model_id=em.id,
                    status=IndexingStatus.NOT_STARTED,
                    priority=rng.choice([0, 0, 10, 20]),
                    seconds_old=rng.randint(1, 600),
                )
                seeded_ids.append(a.id)
                seeded_source_for[a.id] = conn.source.value
                seeded_cc_pair_for[a.id] = (
                    a.connector_id,
                    a.credential_id,
                    a.embedding_model_id,
                )
            db.commit()

        # Simulate prior-tick existing_jobs: pick up to 2 attempts that
        # form a VALID prior state (respect cap + cc-pair invariants).
        # Without this, the soak would feed kickoff a precondition that
        # already violates the invariants, and the post-tick check would
        # flag a "violation" that isn't actually a scheduler bug.
        in_flight_ids: set[int] = set()
        in_flight_per_source: dict[str, int] = {}
        in_flight_cc_pairs: set[tuple[int | None, int | None, int]] = set()
        shuffled = list(seeded_ids)
        rng.shuffle(shuffled)
        for aid in shuffled:
            if rng.random() > 0.4:  # 40% chance to include
                continue
            src = seeded_source_for[aid]
            cc = seeded_cc_pair_for[aid]
            if cap > 0 and in_flight_per_source.get(src, 0) >= cap:
                continue
            if cc in in_flight_cc_pairs:
                continue
            in_flight_ids.add(aid)
            in_flight_per_source[src] = in_flight_per_source.get(src, 0) + 1
            in_flight_cc_pairs.add(cc)
            if len(in_flight_ids) >= 2:
                break
        existing_jobs = {aid: _RecordedJob(job_id=aid) for aid in in_flight_ids}

        primary, _ = run_one_tick(cap=cap, existing_jobs=existing_jobs)
        submitted = primary.submitted_attempt_ids() & set(seeded_ids)

        # Invariant 1: per-source dispatched count + in-flight count ≤ cap.
        per_source: dict[str, int] = {}
        for aid in submitted | in_flight_ids:
            src = seeded_source_for.get(aid)
            if src is None:
                continue
            per_source[src] = per_source.get(src, 0) + 1
        if cap > 0:
            for src, count in per_source.items():
                if count > cap:
                    violations += 1
                    failed(
                        f"iter {it}: source {src} count={count} > cap={cap}",
                        f"submitted={sorted(submitted)} in_flight={sorted(in_flight_ids)}",
                    )

        # Invariant 2: no two of (submitted ∪ in_flight) share a cc-pair.
        seen_cc: dict[tuple[int | None, int | None, int], int] = {}
        for aid in submitted | in_flight_ids:
            key = seeded_cc_pair_for.get(aid)
            if key is None:
                continue
            if key in seen_cc:
                violations += 1
                failed(
                    f"iter {it}: cc-pair {key} held by both {seen_cc[key]} and {aid}"
                )
            seen_cc[key] = aid

    if violations == 0:
        passed(f"{iterations} iterations — invariants held every tick")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_test_data() -> None:
    """Drop everything tagged with SCHEDULER_PREFIX, FK-safe order."""
    engine = get_sqlalchemy_engine()
    with Session(engine) as db:
        db.execute(
            text(
                """
                DELETE FROM index_attempt
                WHERE connector_id IN (
                    SELECT id FROM connector WHERE name LIKE :p
                )
                """
            ),
            {"p": f"{SCHEDULER_PREFIX}%"},
        )
        db.execute(
            text(
                """
                DELETE FROM permission_sync_run
                WHERE cc_pair_id IN (
                    SELECT id FROM connector_credential_pair WHERE name LIKE :p
                )
                """
            ),
            {"p": f"{SCHEDULER_PREFIX}%"},
        )
        db.execute(
            text("DELETE FROM connector_credential_pair WHERE name LIKE :p"),
            {"p": f"{SCHEDULER_PREFIX}%"},
        )
        db.execute(
            text("DELETE FROM connector WHERE name LIKE :p"),
            {"p": f"{SCHEDULER_PREFIX}%"},
        )
        # credentials we created have no name tag — they have no FK to
        # anything tagged after the cc-pair delete, so we leave them.
        # The cc-pair / connector deletes are FK-bound so a residual
        # credential row is harmless.
        db.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


THIS_DIR = Path(__file__).resolve().parent


def precheck_indexer_not_running(force: bool) -> None:
    result = subprocess.run(
        ["pgrep", "-f", "danswer/background/update.py"],
        capture_output=True,
        text=True,
    )
    pids = [p for p in result.stdout.strip().splitlines() if p]
    if pids and not force:
        sys.exit(
            f"\nThe production indexer is running (pids: {pids}). It would race\n"
            "on this script's seeded NOT_STARTED rows and clobber the assertions.\n"
            "Stop it first:\n"
            "  pkill -f 'danswer/background/update.py'\n"
            "Or re-run with --force-with-indexer-running to override (your\n"
            "test attempts may then be picked up by the real scheduler before\n"
            "this script gets to assert on them — expect noise).\n"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes", action="store_true", help="Skip the destructive-op confirmation."
    )
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="Don't clean tagged data at the end (debugging).",
    )
    parser.add_argument(
        "--soak-iterations",
        type=int,
        default=20,
        help="Number of randomized ticks for the soak phase. Default 20.",
    )
    parser.add_argument(
        "--force-with-indexer-running",
        action="store_true",
        help="Run even if the production indexer process is detected.",
    )
    args = parser.parse_args()

    precheck_indexer_not_running(force=args.force_with_indexer_running)

    engine = get_sqlalchemy_engine()
    safe_url = (
        f"{engine.url.drivername}://{engine.url.username}@"
        f"{engine.url.host}:{engine.url.port}/{engine.url.database}"
    )
    print(f"Target DB: {safe_url}")
    if not args.yes:
        ans = input(
            "This will create + delete tagged rows under "
            f"prefix '{SCHEDULER_PREFIX}'. Continue? [y/N] "
        )
        if ans.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    # Always clean before we start so a previous failed run can't pollute
    # the per-source counts (e.g. a left-behind IN_PROGRESS slack row).
    cleanup_test_data()

    # Each phase calls cleanup_test_data() before running so it starts
    # against an empty tagged-row state. Without this, deferred
    # NOT_STARTED rows from earlier phases would compete with later
    # phases' attempts for cap slots and skew assertions.
    phases = [
        phase_a_priority_under_cap,
        phase_b_cap_leak_regression,
        phase_c_cc_pair_guard_with_in_progress,
        phase_d_different_sources_independent_caps,
        phase_e_same_cc_pair_double_submit_prevented,
        phase_g_completed_in_flight_does_not_block,
        phase_h_failed_in_flight_does_not_block,
        phase_i_in_progress_dispatched_not_double_counted,
        lambda: phase_f_soak_random_ticks(args.soak_iterations),
    ]
    try:
        for phase_fn in phases:
            cleanup_test_data()
            phase_fn()
    finally:
        if not args.keep_data:
            cleanup_test_data()
            print("\n  ok  cleaned up tagged rows")
        else:
            print(f"\n  -- kept tagged rows (--keep-data); prefix={SCHEDULER_PREFIX}")

    if _FAILED:
        print(f"\nFAIL: {_FAILED} assertion(s) failed")
        return 1
    print("\nALL PHASES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
