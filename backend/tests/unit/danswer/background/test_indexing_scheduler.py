"""Unit tests for the indexing scheduler's per-source cap and per-cc-pair
collision guard, including the cap-leak fix where attempts already
dispatched to Dask but still NOT_STARTED in the DB must be counted
against the cap.

The scheduler decision logic is exercised through two pure helpers
exposed by ``danswer.background.update``:

  * ``_build_running_view`` — folds DB-IN_PROGRESS rows + already-
    dispatched-but-pre-completion rows into the per-source counter
    and per-cc-pair key set.
  * ``_evaluate_dispatch_for_attempt`` — pure decision: dispatch or
    defer (per-cc-pair collision / per-source cap).

We verify:
  1. Priority sort is honored when the scheduler iterates candidates.
  2. Per-source cap is enforced.
  3. Per-cc-pair guard blocks a same-cc-pair Re-Index collision.
  4. THE BUG FIX: a dispatched-but-not-yet-IN_PROGRESS attempt counts
     against the cap; this prevents the cap leak across scheduler ticks.
  5. Same-tick fairness: two same-source NOT_STARTED rows in one tick
     don't both get dispatched if cap=1.
  6. Same-tick fairness for same-cc-pair NOT_STARTED rows.

Plus a randomized fuzz test that runs the scheduler many ticks against
random NOT_STARTED queues, simulating Dask delays, worker crashes, and
priority bumps. The invariant: across all ticks, the number of attempts
running concurrently for any source is never above ``PER_SOURCE_CAP``,
and a same-cc-pair pair never runs two attempts concurrently.
"""
from __future__ import annotations

import os
import random
import unittest
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from unittest import mock

from danswer.background.update import _build_running_view
from danswer.background.update import _DEFER_CC_PAIR
from danswer.background.update import _DEFER_SOURCE_CAP
from danswer.background.update import _DISPATCH
from danswer.background.update import _evaluate_dispatch_for_attempt


class _Source(str, Enum):
    SLACK = "slack"
    GITHUB = "github"
    CONFLUENCE = "confluence"
    SALESFORCE = "salesforce"
    WEB = "web"


@dataclass
class _FakeConnector:
    name: str
    source: _Source


@dataclass
class _FakeAttempt:
    """Minimal stand-in for ``IndexAttempt`` used by the helpers under
    test. The real ORM object exposes the same attributes the helpers
    touch (``id``, ``connector``, ``connector_id``, ``credential_id``,
    ``embedding_model_id``, ``indexing_priority``)."""

    id: int
    connector_id: int | None
    credential_id: int | None
    embedding_model_id: int
    indexing_priority: int = 0
    connector: _FakeConnector | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attempt(
    *,
    id: int,
    source: _Source = _Source.SLACK,
    connector_id: int | None = None,
    credential_id: int = 1,
    embedding_model_id: int = 2,
    indexing_priority: int = 0,
    name: str | None = None,
) -> _FakeAttempt:
    """Construct a fake attempt with sensible defaults.

    ``connector_id`` defaults to ``id`` so each attempt sits on its own
    cc-pair unless the caller overrides it.
    """
    cid = id if connector_id is None else connector_id
    return _FakeAttempt(
        id=id,
        connector_id=cid,
        credential_id=credential_id,
        embedding_model_id=embedding_model_id,
        indexing_priority=indexing_priority,
        connector=_FakeConnector(
            name=name or f"conn-{cid}-{source.value}", source=source
        ),
    )


def _simulate_tick(
    candidates: list[_FakeAttempt],
    in_progress: list[_FakeAttempt],
    dispatched_pre_completion: list[_FakeAttempt],
    per_source_cap: int,
) -> tuple[list[int], list[tuple[int, str]]]:
    """Run one scheduler tick using only the public helpers.

    Returns a tuple ``(dispatched_ids, deferred)`` where ``deferred`` is
    a list of ``(attempt_id, reason)`` tuples and ``reason`` is one of
    the helper module's ``_DEFER_*`` sentinels.
    """
    running_per_source, in_progress_cc_pair_keys = _build_running_view(
        in_progress, dispatched_pre_completion, per_source_cap
    )
    dispatched: list[int] = []
    deferred: list[tuple[int, str]] = []
    for attempt in candidates:
        decision = _evaluate_dispatch_for_attempt(
            attempt,
            running_per_source,
            in_progress_cc_pair_keys,
            per_source_cap,
        )
        if decision == _DISPATCH:
            dispatched.append(attempt.id)
        else:
            deferred.append((attempt.id, decision))
    return dispatched, deferred


# ---------------------------------------------------------------------------
# Targeted scenarios
# ---------------------------------------------------------------------------


class TestSchedulerDecisions(unittest.TestCase):
    def test_priority_order_is_respected_under_cap(self) -> None:
        """Cap=1 + 3 same-source candidates → highest-priority wins.

        Mirrors the user's reported scenario: 3 Slack connectors, one
        priority-bumped to 20. The bumped attempt must be dispatched
        first; the others defer.
        """
        # Pre-sorted as the real `get_not_started_index_attempts` does:
        # priority DESC, time_created ASC.
        candidates = [
            _attempt(id=928, source=_Source.SLACK, indexing_priority=20),
            _attempt(id=925, source=_Source.SLACK, indexing_priority=0),
            _attempt(id=930, source=_Source.SLACK, indexing_priority=0),
        ]
        dispatched, deferred = _simulate_tick(
            candidates, in_progress=[], dispatched_pre_completion=[], per_source_cap=1
        )
        self.assertEqual(dispatched, [928])
        self.assertEqual(
            sorted(deferred), [(925, _DEFER_SOURCE_CAP), (930, _DEFER_SOURCE_CAP)]
        )

    def test_cap_zero_disables_per_source_limit(self) -> None:
        candidates = [
            _attempt(id=1, source=_Source.SLACK),
            _attempt(id=2, source=_Source.SLACK),
            _attempt(id=3, source=_Source.SLACK),
        ]
        dispatched, deferred = _simulate_tick(
            candidates, in_progress=[], dispatched_pre_completion=[], per_source_cap=0
        )
        self.assertEqual(dispatched, [1, 2, 3])
        self.assertEqual(deferred, [])

    def test_per_source_cap_2_allows_two_same_source(self) -> None:
        candidates = [
            _attempt(id=1, source=_Source.SLACK),
            _attempt(id=2, source=_Source.SLACK),
            _attempt(id=3, source=_Source.SLACK),
        ]
        dispatched, deferred = _simulate_tick(
            candidates, in_progress=[], dispatched_pre_completion=[], per_source_cap=2
        )
        self.assertEqual(dispatched, [1, 2])
        self.assertEqual(deferred, [(3, _DEFER_SOURCE_CAP)])

    def test_different_sources_do_not_share_cap(self) -> None:
        candidates = [
            _attempt(id=1, source=_Source.SLACK),
            _attempt(id=2, source=_Source.GITHUB),
            _attempt(id=3, source=_Source.CONFLUENCE),
            _attempt(id=4, source=_Source.SALESFORCE),
        ]
        dispatched, _ = _simulate_tick(
            candidates, in_progress=[], dispatched_pre_completion=[], per_source_cap=1
        )
        self.assertEqual(dispatched, [1, 2, 3, 4])

    def test_per_cc_pair_guard_blocks_reindex_collision(self) -> None:
        """Same cc-pair has an IN_PROGRESS attempt → re-index attempt
        defers (no FAILED row, just NOT_STARTED waiting)."""
        running = _attempt(
            id=900, source=_Source.SLACK, connector_id=7, credential_id=5
        )
        reindex = _attempt(
            id=940, source=_Source.SLACK, connector_id=7, credential_id=5
        )
        dispatched, deferred = _simulate_tick(
            [reindex],
            in_progress=[running],
            dispatched_pre_completion=[],
            per_source_cap=2,
        )
        self.assertEqual(dispatched, [])
        self.assertEqual(deferred, [(940, _DEFER_CC_PAIR)])

    def test_per_cc_pair_guard_blocks_same_tick_collision(self) -> None:
        """Two NOT_STARTED rows for the SAME cc-pair in one tick: only
        the first dispatches; the second defers via the in-tick set."""
        a = _attempt(id=1, source=_Source.SLACK, connector_id=7, credential_id=5)
        b = _attempt(id=2, source=_Source.SLACK, connector_id=7, credential_id=5)
        dispatched, deferred = _simulate_tick(
            [a, b], in_progress=[], dispatched_pre_completion=[], per_source_cap=2
        )
        self.assertEqual(dispatched, [1])
        self.assertEqual(deferred, [(2, _DEFER_CC_PAIR)])

    # ---------------- THE BUG-FIX REGRESSION ------------------------------

    def test_dispatched_pre_completion_counts_toward_source_cap(self) -> None:
        """Bug fix: when an attempt is sitting in Dask's queue (in
        ``existing_jobs``) but its DB row is still NOT_STARTED, it must
        still hold its source-cap slot.

        Pre-fix: the DB-IN_PROGRESS query returned 0 slack rows; the
        cap read 0 < 1; the next slack candidate leaked through and
        Dask got two slack tasks.
        """
        dispatched_but_unborn = _attempt(id=928, source=_Source.SLACK)
        candidate = _attempt(id=925, source=_Source.SLACK)
        dispatched, deferred = _simulate_tick(
            [candidate],
            in_progress=[],
            dispatched_pre_completion=[dispatched_but_unborn],
            per_source_cap=1,
        )
        self.assertEqual(dispatched, [])
        self.assertEqual(deferred, [(925, _DEFER_SOURCE_CAP)])

    def test_dispatched_pre_completion_counts_for_cc_pair_guard(self) -> None:
        """Same fix: a dispatched-but-not-IN_PROGRESS attempt also
        protects its cc-pair against same-tick re-submission."""
        dispatched_attempt = _attempt(
            id=900, source=_Source.SLACK, connector_id=7, credential_id=5
        )
        candidate = _attempt(
            id=940, source=_Source.SLACK, connector_id=7, credential_id=5
        )
        dispatched, deferred = _simulate_tick(
            [candidate],
            in_progress=[],
            dispatched_pre_completion=[dispatched_attempt],
            per_source_cap=4,
        )
        self.assertEqual(dispatched, [])
        self.assertEqual(deferred, [(940, _DEFER_CC_PAIR)])

    def test_no_double_count_when_attempt_in_both_lists(self) -> None:
        """If a dispatched attempt has just flipped to IN_PROGRESS, it
        may appear in both ``in_progress`` and the dispatched list. The
        accounting must dedupe on attempt id."""
        a = _attempt(id=10, source=_Source.SLACK)
        running_per_source, _ = _build_running_view([a], [a], per_source_cap=2)
        self.assertEqual(running_per_source.get("slack", 0), 1)

    def test_connector_null_attempt_is_skipped_in_view(self) -> None:
        """An attempt whose connector was deleted under us must not
        crash accounting and must not consume a cap slot."""
        broken = _FakeAttempt(
            id=99,
            connector_id=None,
            credential_id=None,
            embedding_model_id=2,
            connector=None,
        )
        running_per_source, keys = _build_running_view([broken], [], per_source_cap=1)
        self.assertEqual(running_per_source, {})
        self.assertEqual(keys, set())

    def test_completed_attempt_not_in_dispatched_list(self) -> None:
        """The caller is responsible for filtering SUCCESS/FAILED rows
        out of the dispatched list (the production query does so).
        Sanity check: when filtered out, they don't consume the cap."""
        # Caller filters before passing in; here we verify the helper
        # treats whatever it receives as still-pre-completion.
        candidates = [_attempt(id=1, source=_Source.SLACK)]
        dispatched, _ = _simulate_tick(
            candidates,
            in_progress=[],
            dispatched_pre_completion=[],  # caller filtered out completed
            per_source_cap=1,
        )
        self.assertEqual(dispatched, [1])


# ---------------------------------------------------------------------------
# Multi-tick stress / fuzz
# ---------------------------------------------------------------------------


@dataclass
class _SimState:
    """Simulator for the cross-tick interaction between scheduler,
    Dask queue, and DB IN_PROGRESS state.

    We model:
      * NOT_STARTED queue (sorted by priority DESC, time_created ASC).
      * Dispatched-pre-completion: attempts the scheduler has handed to
        Dask but which haven't flipped to IN_PROGRESS yet (Dask queueing
        delay or worker spinning up).
      * IN_PROGRESS: attempts a worker has picked up.
      * Done: attempts that finished.

    Each tick:
      1. Build the running view from `in_progress + dispatched`.
      2. For each NOT_STARTED candidate (in priority/FIFO order), the
         scheduler decides dispatch / defer-cc-pair / defer-source-cap.
      3. Newly dispatched attempts move to `dispatched_pre_completion`.
      4. The simulator advances Dask state: each dispatched attempt
         flips to IN_PROGRESS with `flip_prob`, simulating Dask actually
         giving it to a worker.
      5. Each IN_PROGRESS attempt finishes with `finish_prob`.
      6. With probability `crash_prob`, an IN_PROGRESS attempt is
         abruptly killed (worker crash) — it returns to NOT_STARTED, no
         FAILED row (matching the production revert behaviour).

    Across all ticks, two invariants must hold:
      A. For each source, at most `per_source_cap` attempts are
         IN_PROGRESS simultaneously.
      B. No two IN_PROGRESS attempts share the same cc-pair tuple.
    """

    candidates: list[_FakeAttempt]  # NOT_STARTED queue
    dispatched: list[_FakeAttempt] = field(default_factory=list)
    in_progress: list[_FakeAttempt] = field(default_factory=list)
    done: list[int] = field(default_factory=list)
    per_source_cap: int = 1
    flip_prob: float = 0.7
    finish_prob: float = 0.4
    crash_prob: float = 0.05
    rng: random.Random = field(default_factory=random.Random)

    def _sort_candidates(self) -> None:
        self.candidates.sort(
            key=lambda a: (-a.indexing_priority, a.id)  # FIFO via id as tiebreak
        )

    def step(self) -> None:
        self._sort_candidates()
        running_per_source, in_progress_cc_pair_keys = _build_running_view(
            self.in_progress, self.dispatched, self.per_source_cap
        )
        next_candidates: list[_FakeAttempt] = []
        for attempt in self.candidates:
            decision = _evaluate_dispatch_for_attempt(
                attempt,
                running_per_source,
                in_progress_cc_pair_keys,
                self.per_source_cap,
            )
            if decision == _DISPATCH:
                self.dispatched.append(attempt)
            else:
                next_candidates.append(attempt)
        self.candidates = next_candidates

        # Dask: dispatched → IN_PROGRESS.
        still_dispatched: list[_FakeAttempt] = []
        for d in self.dispatched:
            if self.rng.random() < self.flip_prob:
                self.in_progress.append(d)
            else:
                still_dispatched.append(d)
        self.dispatched = still_dispatched

        # Workers: IN_PROGRESS → done OR crash.
        still_running: list[_FakeAttempt] = []
        for ip in self.in_progress:
            r = self.rng.random()
            if r < self.finish_prob:
                self.done.append(ip.id)
            elif r < self.finish_prob + self.crash_prob:
                # Crash: revert to NOT_STARTED queue (matches the
                # worker-side advisory-lock revert path).
                self.candidates.append(ip)
            else:
                still_running.append(ip)
        self.in_progress = still_running

    def assert_invariants(self) -> None:
        per_source: dict[str, int] = {}
        cc_pair_keys: set[tuple[int | None, int | None, int]] = set()
        for ip in self.in_progress:
            assert ip.connector is not None
            key = (ip.connector_id, ip.credential_id, ip.embedding_model_id)
            assert (
                key not in cc_pair_keys
            ), f"two IN_PROGRESS attempts share cc-pair {key}"
            cc_pair_keys.add(key)
            per_source[ip.connector.source.value] = (
                per_source.get(ip.connector.source.value, 0) + 1
            )
        if self.per_source_cap > 0:
            for src, count in per_source.items():
                assert count <= self.per_source_cap, (
                    f"per-source cap leaked for {src}: "
                    f"{count} IN_PROGRESS > cap={self.per_source_cap}"
                )


class TestSchedulerStress(unittest.TestCase):
    """Fuzz tests. The randomized seed is cycled so failures are
    reproducible: each iteration prints its seed if the assertion
    trips, and the test runs N iterations."""

    NUM_ITERATIONS = 200

    def _build_random_queue(
        self, rng: random.Random, n: int, sources: list[_Source]
    ) -> list[_FakeAttempt]:
        # cc-pairs are (connector_id, credential_id, embedding_model_id).
        # We pick connector_id from a small pool so collisions actually
        # happen (the user's bug had multiple Slack cc-pairs sharing the
        # same credential).
        connector_pool = list(range(1, 8))
        attempts: list[_FakeAttempt] = []
        for i in range(n):
            src = rng.choice(sources)
            attempts.append(
                _attempt(
                    id=i + 1,
                    source=src,
                    connector_id=rng.choice(connector_pool),
                    credential_id=rng.choice([1, 2, 3]),
                    indexing_priority=rng.choice([0, 0, 0, 10, 20, 30]),
                )
            )
        return attempts

    def test_fuzz_invariants_hold_across_random_ticks(self) -> None:
        """Run NUM_ITERATIONS independent simulations and assert that
        invariants A (per-source cap) and B (cc-pair uniqueness) hold
        every tick. Each simulation gets a fresh seed so failures are
        reproducible."""
        sources = list(_Source)
        for seed in range(self.NUM_ITERATIONS):
            rng = random.Random(seed)
            cap = rng.choice([1, 1, 1, 2, 3])  # cap=1 weighted (real default)
            n_attempts = rng.randint(5, 30)
            ticks = rng.randint(20, 60)

            sim = _SimState(
                candidates=self._build_random_queue(rng, n_attempts, sources),
                per_source_cap=cap,
                flip_prob=rng.uniform(0.4, 0.9),
                finish_prob=rng.uniform(0.2, 0.6),
                crash_prob=rng.uniform(0.0, 0.1),
                rng=rng,
            )

            for tick in range(ticks):
                try:
                    sim.step()
                    sim.assert_invariants()
                except AssertionError as e:
                    self.fail(
                        f"invariant violated: seed={seed} tick={tick} "
                        f"cap={cap} n={n_attempts}: {e}"
                    )

    def test_priority_strict_order_under_cap_one(self) -> None:
        """Targeted: with cap=1 and one source bucket, attempts MUST
        finish in priority-DESC, FIFO order. No lower-priority can
        sneak ahead via the cap-leak path."""
        for seed in range(self.NUM_ITERATIONS):
            rng = random.Random(seed)
            # Build a fixed-source queue so cap=1 fully serializes it.
            n = rng.randint(4, 12)
            attempts: list[_FakeAttempt] = []
            for i in range(n):
                attempts.append(
                    _attempt(
                        id=i + 1,
                        source=_Source.SLACK,
                        connector_id=i + 1,  # distinct cc-pairs
                        indexing_priority=rng.choice([0, 5, 10, 20, 30]),
                    )
                )
            sim = _SimState(
                candidates=list(attempts),
                per_source_cap=1,
                flip_prob=rng.uniform(0.5, 0.9),
                finish_prob=rng.uniform(0.3, 0.7),
                crash_prob=0.0,  # no crashes — we want clean priority order
                rng=rng,
            )
            # Run until everything completes.
            max_ticks = 500
            for _ in range(max_ticks):
                sim.step()
                if not sim.candidates and not sim.dispatched and not sim.in_progress:
                    break
            else:
                self.fail(f"sim did not drain in {max_ticks} ticks (seed={seed})")

            expected_order = sorted(
                attempts, key=lambda a: (-a.indexing_priority, a.id)
            )
            expected_ids = [a.id for a in expected_order]
            self.assertEqual(
                sim.done,
                expected_ids,
                f"priority order violated (seed={seed}): "
                f"got {sim.done}, expected {expected_ids}",
            )

    def test_user_reported_scenario(self) -> None:
        """Exact replay of the user-observed setup:

          * 7 Slack connectors (cc-pairs).
          * One has its attempt priority bumped to 20 (the rest at 0).
          * cap = 1.
          * Two Dask workers (so the cap-leak bug is exercisable).

        Pre-fix: the leak path could let a prio=0 attempt run while
        the prio=20 attempt sat NOT_STARTED. With the fix, no prio=0
        attempt may finish before the prio=20 one.
        """
        slack_attempts = [
            _attempt(id=i + 1, source=_Source.SLACK, connector_id=i + 1)
            for i in range(7)
        ]
        # Bump #4 to priority 20.
        slack_attempts[3].indexing_priority = 20
        bumped_id = slack_attempts[3].id

        for seed in range(self.NUM_ITERATIONS):
            rng = random.Random(seed)
            sim = _SimState(
                candidates=[
                    _FakeAttempt(
                        id=a.id,
                        connector_id=a.connector_id,
                        credential_id=a.credential_id,
                        embedding_model_id=a.embedding_model_id,
                        indexing_priority=a.indexing_priority,
                        connector=a.connector,
                    )
                    for a in slack_attempts
                ],
                per_source_cap=1,
                # Skew flip_prob low (~ Dask queue delay) to maximally
                # exercise the cap-leak path.
                flip_prob=rng.uniform(0.2, 0.6),
                finish_prob=rng.uniform(0.3, 0.6),
                crash_prob=0.0,
                rng=rng,
            )
            max_ticks = 500
            for _ in range(max_ticks):
                sim.step()
                if not sim.candidates and not sim.dispatched and not sim.in_progress:
                    break
            else:
                self.fail(f"sim did not drain (seed={seed})")

            # The bumped attempt must finish FIRST.
            self.assertEqual(
                sim.done[0],
                bumped_id,
                f"priority-20 attempt did not finish first (seed={seed}): "
                f"order was {sim.done}",
            )

    def test_buggy_view_without_fix_leaks_cap_regression_guard(self) -> None:
        """Regression guard: simulate the pre-fix codepath (build view
        from IN_PROGRESS only, ignoring dispatched-but-pre-completion)
        and assert the invariants DO get violated. This proves the test
        harness is sensitive enough to catch a regression — if someone
        removes the dispatched-row accounting from
        ``_build_running_view``, the targeted scenario test above would
        start failing.
        """

        def _buggy_view(
            in_progress: list[_FakeAttempt],
            _dispatched_ignored: list[_FakeAttempt],
            per_source_cap: int,
        ) -> tuple[dict[str, int], set[tuple[int | None, int | None, int]]]:
            # Pre-fix accounting: only DB IN_PROGRESS rows count.
            return _build_running_view(in_progress, [], per_source_cap)

        observed_concurrent_violation = False
        slack_attempts = [
            _attempt(id=i + 1, source=_Source.SLACK, connector_id=i + 1)
            for i in range(7)
        ]
        slack_attempts[3].indexing_priority = 20

        # Use a fixed seed where Dask flip_prob is low enough that a
        # dispatched attempt sits queued for at least one tick (the
        # exact window the cap-leak exploits).
        rng = random.Random(42)
        candidates = [
            _FakeAttempt(
                id=a.id,
                connector_id=a.connector_id,
                credential_id=a.credential_id,
                embedding_model_id=a.embedding_model_id,
                indexing_priority=a.indexing_priority,
                connector=a.connector,
            )
            for a in slack_attempts
        ]
        in_progress: list[_FakeAttempt] = []
        dispatched: list[_FakeAttempt] = []
        cap = 1
        for _ in range(80):
            running_per_source, in_progress_cc_pair_keys = _buggy_view(
                in_progress, dispatched, cap
            )
            next_candidates: list[_FakeAttempt] = []
            for attempt in sorted(
                candidates, key=lambda a: (-a.indexing_priority, a.id)
            ):
                decision = _evaluate_dispatch_for_attempt(
                    attempt,
                    running_per_source,
                    in_progress_cc_pair_keys,
                    cap,
                )
                if decision == _DISPATCH:
                    dispatched.append(attempt)
                else:
                    next_candidates.append(attempt)
            candidates = next_candidates
            still_dispatched: list[_FakeAttempt] = []
            for d in dispatched:
                if rng.random() < 0.3:  # low flip_prob = real Dask delay
                    in_progress.append(d)
                else:
                    still_dispatched.append(d)
            dispatched = still_dispatched

            # Check the invariant.
            per_source: dict[str, int] = {}
            for ip in in_progress:
                assert ip.connector is not None
                per_source[ip.connector.source.value] = (
                    per_source.get(ip.connector.source.value, 0) + 1
                )
            for src, count in per_source.items():
                if cap > 0 and count > cap:
                    observed_concurrent_violation = True

            # Drain finished attempts so the simulation makes progress.
            in_progress = [ip for ip in in_progress if rng.random() > 0.4]

        self.assertTrue(
            observed_concurrent_violation,
            "Pre-fix simulation did not produce a cap leak — fuzz "
            "harness may have lost sensitivity. Tighten flip_prob/seed "
            "or extend tick count.",
        )

    def test_crash_recovery_does_not_leak_cap(self) -> None:
        """High crash rate (workers dying mid-run) must not leak the
        per-source cap. The crash path returns the attempt to the
        NOT_STARTED queue without inflating the cap counter."""
        for seed in range(self.NUM_ITERATIONS):
            rng = random.Random(seed)
            n = rng.randint(8, 20)
            attempts = self._build_random_queue(rng, n, list(_Source))
            sim = _SimState(
                candidates=attempts,
                per_source_cap=rng.choice([1, 2]),
                flip_prob=rng.uniform(0.4, 0.8),
                finish_prob=rng.uniform(0.2, 0.5),
                crash_prob=rng.uniform(0.1, 0.3),  # crashy
                rng=rng,
            )
            for tick in range(80):
                try:
                    sim.step()
                    sim.assert_invariants()
                except AssertionError as e:
                    self.fail(
                        f"invariant violated under crashy workers: "
                        f"seed={seed} tick={tick}: {e}"
                    )


class TestPerSourceCapOverrides(unittest.TestCase):
    """The per-source override (`INDEXING_PER_SOURCE_CAP_OVERRIDES`) lets a
    single source run at a different cap than the global default without
    affecting other sources. Web is the motivating case: lift its cap while
    Slack/Confluence stay at 1.
    """

    def _tick(
        self,
        candidates: list[_FakeAttempt],
        in_progress: list[_FakeAttempt],
        per_source_cap: int,
        overrides: dict[str, int],
    ) -> tuple[list[int], list[tuple[int, str]]]:
        running_per_source, keys = _build_running_view(
            in_progress, [], per_source_cap, overrides
        )
        dispatched: list[int] = []
        deferred: list[tuple[int, str]] = []
        for attempt in candidates:
            decision = _evaluate_dispatch_for_attempt(
                attempt, running_per_source, keys, per_source_cap, overrides
            )
            if decision == _DISPATCH:
                dispatched.append(attempt.id)
            else:
                deferred.append((attempt.id, decision))
        return dispatched, deferred

    def test_override_uncaps_one_source_only(self) -> None:
        # Default cap 1, web uncapped (0). Three distinct web cc-pairs +
        # two distinct slack cc-pairs queued on an idle scheduler.
        candidates = [
            _attempt(id=1, source=_Source.WEB),
            _attempt(id=2, source=_Source.WEB),
            _attempt(id=3, source=_Source.WEB),
            _attempt(id=4, source=_Source.SLACK),
            _attempt(id=5, source=_Source.SLACK),
        ]
        dispatched, deferred = self._tick(
            candidates, in_progress=[], per_source_cap=1, overrides={"web": 0}
        )
        # All three web attempts go (uncapped); only one slack goes (cap 1).
        self.assertEqual(set(dispatched), {1, 2, 3, 4})
        self.assertEqual(deferred, [(5, _DEFER_SOURCE_CAP)])

    def test_override_with_finite_cap(self) -> None:
        # web=2: at most two web at once; the third defers.
        candidates = [
            _attempt(id=1, source=_Source.WEB),
            _attempt(id=2, source=_Source.WEB),
            _attempt(id=3, source=_Source.WEB),
        ]
        dispatched, deferred = self._tick(
            candidates, in_progress=[], per_source_cap=1, overrides={"web": 2}
        )
        self.assertEqual(set(dispatched), {1, 2})
        self.assertEqual(deferred, [(3, _DEFER_SOURCE_CAP)])

    def test_non_overridden_source_keeps_global_default(self) -> None:
        # Override only names web; confluence must still honor the global 1.
        candidates = [
            _attempt(id=1, source=_Source.CONFLUENCE),
            _attempt(id=2, source=_Source.CONFLUENCE),
        ]
        dispatched, deferred = self._tick(
            candidates, in_progress=[], per_source_cap=1, overrides={"web": 0}
        )
        self.assertEqual(dispatched, [1])
        self.assertEqual(deferred, [(2, _DEFER_SOURCE_CAP)])

    def test_per_cc_pair_lock_still_holds_when_uncapped(self) -> None:
        # Even uncapped, the same cc-pair never runs twice concurrently.
        running = _attempt(id=1, source=_Source.WEB, connector_id=99)
        dup = _attempt(id=2, source=_Source.WEB, connector_id=99)
        dispatched, deferred = self._tick(
            [dup], in_progress=[running], per_source_cap=1, overrides={"web": 0}
        )
        self.assertEqual(dispatched, [])
        self.assertEqual(deferred, [(2, _DEFER_CC_PAIR)])

    def test_resolve_overrides_parsing(self) -> None:
        from danswer.configs.indexing_concurrency import _resolve_overrides

        with mock.patch.dict(
            os.environ,
            {"INDEXING_PER_SOURCE_CAP_OVERRIDES": " web = 0 , slack=2 ,bad,=3,x=y"},
        ):
            self.assertEqual(_resolve_overrides(), {"web": 0, "slack": 2})


if __name__ == "__main__":
    unittest.main()
