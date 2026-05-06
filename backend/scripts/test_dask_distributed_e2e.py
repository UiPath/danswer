"""End-to-end test for the Dask-Distributed background topology.

Spawns a real `dask scheduler` + N `dask worker` subprocesses on the
local machine, exercises the topology with synthetic tasks via the
`distributed.Client` API, and asserts the behaviors that matter for
the indexing-scaling design:

  POSITIVE
    P1  All N workers register with the scheduler within a bounded
        time window.
    P2  M concurrent tasks run in parallel across workers — wall
        time is bounded by ceil(M/N) × per_task_seconds, not M ×
        per_task_seconds. (This is THE assertion proving "multiple
        workers pick work in parallel".)
    P3  Tasks fan out across at least 2 distinct workers when
        M > 1. (Catches a degenerate scheduler that pins everything
        to one worker.)

  NEGATIVE
    N1  Worker death mid-task — surviving workers continue accepting
        new submissions; cluster doesn't deadlock.
    N2  Connecting to a non-existent scheduler fails fast with a
        clear error rather than hanging indefinitely.
    N3  Scheduler death — Client.submit() against a dead scheduler
        raises within a bounded time, doesn't hang.

The test is self-contained (no Postgres/Vespa/model-server needed)
and uses random ports per run so concurrent invocations don't
collide. Pass --runs N to repeat the whole suite N times — useful
for catching flakes.

Usage:
    cd backend
    PYTHONPATH=$(pwd) python scripts/test_dask_distributed_e2e.py [--runs N] [--workers M]

Exits 0 if every run passes, non-zero otherwise.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from contextlib import closing
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dask.distributed import Client


# Path to the `dask` CLI that ships with the same venv we're running
# under. `sys.executable` always points at the active python, even
# when the venv was invoked directly without `source activate` (which
# leaves `.venv/bin` off PATH). Falling back to the bare name lets the
# test still work if `dask` is on PATH for some other reason.
_VENV_BIN = Path(sys.executable).parent
_DASK_CLI = str(_VENV_BIN / "dask") if (_VENV_BIN / "dask").exists() else "dask"


def _subprocess_env() -> dict[str, str]:
    """Env for dask child processes — prepend the venv's bin so the
    `dask` CLI (and anything else it shells out to) is resolvable."""
    env = os.environ.copy()
    env["PATH"] = f"{_VENV_BIN}{os.pathsep}{env.get('PATH', '')}"
    return env


_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"
_INFO = "\033[33mINFO\033[0m"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def ok(msg: str) -> None:
    print(f"  [{_PASS}] {msg}")


def fail(msg: str) -> None:
    print(f"  [{_FAIL}] {msg}")


def info(msg: str) -> None:
    print(f"  [{_INFO}] {msg}")


# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------


def find_free_port() -> int:
    """Pick a random unused TCP port. Used to avoid 8786 collisions
    when the user runs multiple suites concurrently or alongside a
    real dev stack."""
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(host: str, port: int, timeout: float) -> bool:
    """Poll until something accepts on host:port, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with closing(socket.create_connection((host, port), timeout=1.0)):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def start_scheduler(port: int) -> subprocess.Popen:
    """Start a dask scheduler bound to localhost. Dashboard is set to
    a random ephemeral port so it doesn't fight with anything."""
    proc = subprocess.Popen(
        [
            _DASK_CLI,
            "scheduler",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--dashboard-address",
            ":0",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_subprocess_env(),
    )
    if not wait_for_port("127.0.0.1", port, timeout=20.0):
        proc.kill()
        raise RuntimeError(f"scheduler did not bind 127.0.0.1:{port} within 20s")
    return proc


def start_worker(scheduler_addr: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            _DASK_CLI,
            "worker",
            scheduler_addr,
            "--nworkers=1",
            "--nthreads=1",
            "--memory-limit=1GB",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_subprocess_env(),
    )


def kill(proc: subprocess.Popen, grace_seconds: float = 3.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()


@contextmanager
def cluster(num_workers: int) -> Iterator[tuple[str, list[subprocess.Popen]]]:
    """Bring up a scheduler + N workers, hand back (addr, worker_procs).
    Tears everything down on exit, even on exception."""
    sched_port = find_free_port()
    scheduler_addr = f"tcp://127.0.0.1:{sched_port}"
    sched_proc = start_scheduler(sched_port)
    workers: list[subprocess.Popen] = []
    try:
        for _ in range(num_workers):
            workers.append(start_worker(scheduler_addr))
        yield scheduler_addr, workers
    finally:
        for w in workers:
            kill(w)
        kill(sched_proc)


def wait_for_workers(client: Client, expected: int, timeout: float) -> int:
    """Poll the scheduler until it reports `expected` workers (or
    timeout). Returns the actual count seen at the end."""
    deadline = time.monotonic() + timeout
    last = 0
    while time.monotonic() < deadline:
        last = len(client.scheduler_info()["workers"])
        if last >= expected:
            return last
        time.sleep(0.5)
    return last


# ---------------------------------------------------------------------------
# Synthetic tasks (run inside dask-worker subprocesses)
# ---------------------------------------------------------------------------


def _sleep_task(duration: float) -> str:
    """Sleep + return the worker's hostname so we can verify
    distribution. Defined at module level so Dask can pickle it."""
    import socket as _socket
    import time as _time

    _time.sleep(duration)
    return _socket.gethostname()


def _quick_task(x: int) -> int:
    """Trivial task to verify task plumbing without sleeping."""
    return x * 2


# ---------------------------------------------------------------------------
# Test phases
# ---------------------------------------------------------------------------


def phase_setup(num_workers: int, scheduler_addr: str) -> tuple[Client, bool]:
    """P1: every worker registers with the scheduler within a bounded
    window. Returns (client, ok_flag)."""
    section("Phase 1 — workers register with scheduler")
    try:
        client = Client(scheduler_addr, timeout=10)
    except Exception as e:
        fail(f"could not connect to scheduler: {e}")
        return None, False  # type: ignore[return-value]
    seen = wait_for_workers(client, expected=num_workers, timeout=20.0)
    if seen >= num_workers:
        ok(f"scheduler reports {seen} worker(s) registered")
        return client, True
    fail(
        f"only {seen}/{num_workers} workers registered after 20s; "
        "did the workers crash on startup?"
    )
    return client, False


def phase_parallelism(client: Client, num_workers: int) -> bool:
    """P2: M concurrent tasks should run in parallel.

    With M = 2 × num_workers and per-task sleep = 3s, sequential time
    is M × 3 = 6×num_workers seconds; parallel time is 2 × 3 = 6
    seconds (plus scheduler overhead). We bound the wall time at
    `ceil(M/N) × per_task + slack` and assert.
    """
    section("Phase 2 — concurrent tasks run in parallel")
    per_task = 3.0
    num_tasks = num_workers * 2
    expected_parallel_time = (num_tasks / num_workers) * per_task
    # Allow generous overhead: scheduler dispatch, Python startup,
    # GC, CI noise. 5s slack is plenty in practice.
    upper_bound = expected_parallel_time + 5.0

    start = time.monotonic()
    futures = [client.submit(_sleep_task, per_task, pure=False) for _ in range(num_tasks)]
    # gather() blocks until all are done; raises if any failed.
    try:
        results = client.gather(futures)
    except Exception as e:
        fail(f"gather() raised: {e}")
        return False
    elapsed = time.monotonic() - start

    if elapsed <= upper_bound:
        ok(
            f"{num_tasks} tasks × {per_task}s each finished in "
            f"{elapsed:.1f}s (bound {upper_bound:.1f}s)"
        )
        info(
            f"sequential lower bound would be {num_tasks * per_task:.1f}s; "
            f"parallelism is real."
        )
        info(f"task return values (worker hostnames): {sorted(set(results))[:5]!r}")
        return True
    fail(
        f"{num_tasks} tasks took {elapsed:.1f}s, expected <{upper_bound:.1f}s. "
        "Tasks may be running sequentially — check that --nthreads=1 "
        "isn't pinning everything to one worker."
    )
    return False


def phase_distribution(client: Client) -> bool:
    """P3: tasks land on at least 2 distinct workers."""
    section("Phase 3 — tasks distribute across workers")
    futures = [client.submit(_sleep_task, 0.2, pure=False) for _ in range(20)]
    results = client.gather(futures)
    distinct_workers = set(results)
    # `_sleep_task` returns hostname; in a single-host test all
    # workers share a hostname. So instead of hostname-cardinality
    # we ask the scheduler directly which workers ran tasks.
    who_has = client.scheduler_info()["workers"]
    workers_used = set()
    for fut in futures:
        try:
            who = client.who_has(fut).get(fut.key, ())
            workers_used.update(who)
        except Exception:
            pass
    used_count = len(workers_used) if workers_used else len(distinct_workers)
    if used_count >= 2:
        ok(f"work spread across {used_count} workers (out of {len(who_has)})")
        return True
    info(
        f"only {used_count} worker(s) used — possibly all tasks finished too "
        "fast for the scheduler to spread, or the cluster is single-worker."
    )
    # Don't hard-fail this with N=1 worker (degenerate); only fail if
    # we expected spread.
    return len(who_has) < 2 or False


def phase_worker_death(
    client: Client, workers: list[subprocess.Popen], scheduler_addr: str
) -> bool:
    """N1: kill a worker mid-task; surviving workers continue
    accepting submissions and the scheduler doesn't deadlock."""
    section("Phase 4 — worker death does not deadlock the cluster")
    if len(workers) < 2:
        info("skipping — need ≥2 workers for this test")
        return True

    # Submit a long task on each worker so at least one is busy when
    # we kill it.
    busy_futures = [
        client.submit(_sleep_task, 4.0, pure=False) for _ in range(len(workers))
    ]
    time.sleep(0.5)  # let the scheduler dispatch them

    # Pick the first live worker and kill it.
    victim = None
    for w in workers:
        if w.poll() is None:
            victim = w
            break
    if victim is None:
        fail("no live workers to kill")
        return False
    info(f"killing worker pid={victim.pid} mid-task")
    kill(victim)

    # The future on the killed worker will likely raise. We don't
    # care which one fails; we care that the cluster STAYS USABLE.
    # gather() with errors='skip' returns successful ones.
    for f in busy_futures:
        try:
            f.result(timeout=10.0)
        except Exception:
            pass

    # Cluster usable test: submit a trivial task, gather, must
    # succeed within a few seconds on a surviving worker.
    try:
        result = client.submit(_quick_task, 21, pure=False).result(timeout=10.0)
    except Exception as e:
        fail(f"cluster unusable after worker death: {e}")
        return False
    if result == 42:
        ok("cluster still serves new submissions after a worker died")
        return True
    fail(f"unexpected result {result} from quick task")
    return False


def phase_unreachable_scheduler() -> bool:
    """N2: connecting to a non-existent scheduler fails fast."""
    section("Phase 5 — connecting to a dead scheduler fails fast")
    bogus_port = find_free_port()  # nothing listening here
    bogus_addr = f"tcp://127.0.0.1:{bogus_port}"
    start = time.monotonic()
    try:
        # Short timeout — we'd rather see "couldn't connect" than hang.
        Client(bogus_addr, timeout=3)
    except Exception as e:
        elapsed = time.monotonic() - start
        if elapsed < 8.0:
            ok(
                f"Client({bogus_addr}) raised {type(e).__name__} in "
                f"{elapsed:.1f}s (bounded as expected)"
            )
            return True
        fail(f"Client raised but took {elapsed:.1f}s — too slow for a fail-fast")
        return False
    fail("Client connected to a non-existent scheduler — expected an exception")
    return False


def phase_scheduler_death(client: Client, sched_killer) -> bool:
    """N3: scheduler death is observable to the client. After
    sched_killer() runs, client.submit() must error within a bounded
    time rather than hanging."""
    section("Phase 6 — scheduler death surfaces to client without hanging")
    sched_killer()
    # Give the client a moment to notice the dropped connection.
    time.sleep(2.0)
    start = time.monotonic()
    try:
        f = client.submit(_quick_task, 1, pure=False)
        f.result(timeout=10.0)
    except Exception as e:
        elapsed = time.monotonic() - start
        ok(
            f"submit/result against dead scheduler raised {type(e).__name__} "
            f"in {elapsed:.1f}s (bounded)"
        )
        return True
    fail("submit/result succeeded against a dead scheduler — unexpected")
    return False


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_once(num_workers: int) -> bool:
    """One full pass of all phases. Returns True iff everything passed."""
    print(f"\n{'#' * 60}\n# Run start — {num_workers} workers\n{'#' * 60}")
    sched_port = find_free_port()
    scheduler_addr = f"tcp://127.0.0.1:{sched_port}"
    sched_proc = start_scheduler(sched_port)
    worker_procs: list[subprocess.Popen] = []
    overall_ok = True
    client: Client | None = None
    try:
        for _ in range(num_workers):
            worker_procs.append(start_worker(scheduler_addr))

        client, ok_setup = phase_setup(num_workers, scheduler_addr)
        if not ok_setup:
            return False

        if not phase_parallelism(client, num_workers):
            overall_ok = False

        if not phase_distribution(client):
            overall_ok = False

        if not phase_worker_death(client, worker_procs, scheduler_addr):
            overall_ok = False

        if not phase_unreachable_scheduler():
            overall_ok = False

        # Scheduler-death must run last — it kills the scheduler we
        # were using and we'd have to restart it for any subsequent
        # phase.
        def _kill_scheduler() -> None:
            kill(sched_proc)

        if not phase_scheduler_death(client, _kill_scheduler):
            overall_ok = False

    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        for w in worker_procs:
            kill(w)
        kill(sched_proc)
    return overall_ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="How many times to repeat the full suite (default: 1)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Number of dask-worker subprocesses per run (default: 3)",
    )
    args = parser.parse_args()

    failures: list[int] = []
    for i in range(1, args.runs + 1):
        print(f"\n{'=' * 60}\n=== Run {i}/{args.runs}\n{'=' * 60}")
        try:
            if not run_once(args.workers):
                failures.append(i)
        except Exception as e:
            fail(f"run {i} crashed: {type(e).__name__}: {e}")
            failures.append(i)

    print()
    if not failures:
        print(f"[{_PASS}] dask-distributed e2e: {args.runs} run(s), all passed")
        return 0
    print(
        f"[{_FAIL}] dask-distributed e2e: {len(failures)}/{args.runs} run(s) failed: "
        f"{failures}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
