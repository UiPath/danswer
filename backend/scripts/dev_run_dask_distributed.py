"""Dev helper that spawns the full Dask-Distributed background stack
as plain subprocesses.

Mirrors `dev_run_background_jobs.py` (one parent Python process, child
processes for each background role, Ctrl-C tears down the tree) but
with the prod-shape topology:

  dask-scheduler        TCP RPC + dashboard
       │
       ├── dask-worker × N        actual indexing executors
       ├── indexer-scheduler      runs update.py polling loop, submits
       │                          to dask-scheduler instead of an
       │                          in-process LocalCluster
       ├── celery-worker          unchanged
       └── celery-beat            unchanged

Use this when you want to reproduce production indexing behavior
locally without K8s or Docker. For day-to-day connector-code work,
keep using `dev_run_background_jobs.py` — it's faster to start and
the LocalCluster mode is sufficient for most testing.

Usage:
    cd backend
    PYTHONPATH=$(pwd) python scripts/dev_run_dask_distributed.py
    # ...or with custom worker count:
    PYTHONPATH=$(pwd) python scripts/dev_run_dask_distributed.py \\
        --num-workers 4
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time


SCHEDULER_HOST = "127.0.0.1"


def monitor_process(process_name: str, process: subprocess.Popen) -> None:
    """Stream a child's stdout/stderr to our own stdout with a label."""
    assert process.stdout is not None
    while True:
        output = process.stdout.readline()
        if output:
            print(f"{process_name}: {output.strip()}", flush=True)
        if process.poll() is not None:
            break


def wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    """Poll a TCP port until something accepts connections, or timeout.

    Used to gate dask-worker spawn on the scheduler being reachable —
    without this, workers crash with `ConnectionRefusedError` and have
    to retry on their own backoff.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def spawn(
    name: str,
    cmd: list[str],
    env: dict[str, str] | None = None,
) -> tuple[subprocess.Popen, threading.Thread]:
    """Start a subprocess + a thread tailing its output."""
    process = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    thread = threading.Thread(target=monitor_process, args=(name, process), daemon=True)
    thread.start()
    return process, thread


def run(
    num_workers: int,
    scheduler_port: int,
    dashboard_port: int,
    no_celery: bool,
    no_indexer: bool,
) -> int:
    # Children inherit our env (Postgres / Vespa / GenAI / model-server
    # creds etc.) plus a guaranteed PYTHONPATH=. so that subprocess'd
    # `dask worker` can import `danswer.*` when deserializing the
    # run_indexing_entrypoint callable.
    base_env = os.environ.copy()
    base_env["PYTHONPATH"] = "."

    scheduler_addr = f"tcp://{SCHEDULER_HOST}:{scheduler_port}"
    children: list[subprocess.Popen] = []

    def shutdown(*_args: object) -> None:
        print("\n[dev_run_dask_distributed] Caught signal; shutting down…")
        for proc in children:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        # Give them a moment to terminate cleanly before SIGKILL.
        deadline = time.monotonic() + 5.0
        for proc in children:
            timeout = max(0.1, deadline - time.monotonic())
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # 1. Dask scheduler — must come up first so workers don't have to
    # back off + retry. The bind host is 127.0.0.1 not 0.0.0.0
    # because this is a dev-only helper; nothing should reach it from
    # outside the host.
    print(f"[dev_run_dask_distributed] starting dask-scheduler on {scheduler_addr}")
    sched_proc, _ = spawn(
        "DASK-SCHED",
        [
            "dask",
            "scheduler",
            "--host",
            SCHEDULER_HOST,
            "--port",
            str(scheduler_port),
            "--dashboard-address",
            f":{dashboard_port}",
        ],
        env=base_env,
    )
    children.append(sched_proc)

    if not wait_for_port(SCHEDULER_HOST, scheduler_port, timeout=30.0):
        print(
            f"[dev_run_dask_distributed] scheduler did not bind {scheduler_addr} "
            "within 30s; aborting."
        )
        shutdown()
        return 1
    print(
        "[dev_run_dask_distributed] scheduler is up. "
        f"Dashboard: http://{SCHEDULER_HOST}:{dashboard_port}"
    )

    # 2. Dask workers — N processes, each one thread / one worker, so
    # each gets its own RSS envelope. Same pattern as the K8s
    # `dask-worker-deployment.yaml`.
    worker_env = base_env.copy()
    worker_env["CURRENT_PROCESS_IS_AN_INDEXING_JOB"] = "true"
    for i in range(num_workers):
        proc, _ = spawn(
            f"DASK-WORKER-{i}",
            [
                "dask",
                "worker",
                scheduler_addr,
                "--nworkers=1",
                "--nthreads=1",
                "--memory-limit=4GB",
            ],
            env=worker_env,
        )
        children.append(proc)
    print(f"[dev_run_dask_distributed] started {num_workers} dask-worker(s)")

    # 3. Indexer-scheduler — runs the update.py polling loop and
    # submits work to the scheduler we just started.
    if not no_indexer:
        indexer_env = base_env.copy()
        indexer_env["DASK_SCHEDULER_ADDRESS"] = scheduler_addr
        indexer_env["CURRENT_PROCESS_IS_AN_INDEXING_JOB"] = "true"
        proc, _ = spawn(
            "INDEXER",
            ["python", "danswer/background/update.py"],
            env=indexer_env,
        )
        children.append(proc)
        print("[dev_run_dask_distributed] started indexer-scheduler")

    # 4. Celery worker + beat — unchanged from dev_run_background_jobs.py.
    # Indexing isn't routed through Celery in this fork, so these
    # exist solely to handle prune / sync / retention / cleanup / etc.
    if not no_celery:
        worker_proc, _ = spawn(
            "CELERY-WORKER",
            [
                "celery",
                "-A",
                "ee.danswer.background.celery.celery_app",
                "worker",
                "--pool=threads",
                "--concurrency=10",
                "--loglevel=INFO",
            ],
            env=base_env,
        )
        children.append(worker_proc)

        beat_proc, _ = spawn(
            "CELERY-BEAT",
            [
                "celery",
                "-A",
                "ee.danswer.background.celery.celery_app",
                "beat",
                "--loglevel=INFO",
            ],
            env=base_env,
        )
        children.append(beat_proc)
        print("[dev_run_dask_distributed] started celery worker + beat")

    print(
        "[dev_run_dask_distributed] all processes launched. "
        "Ctrl-C to tear down the whole tree."
    )

    # Block forever, watching for any child to die. If the scheduler
    # or indexer goes down we don't try to recover here (it's a dev
    # helper, not a supervisor) — just exit and let the dev see why.
    try:
        while True:
            for proc in children:
                if proc.poll() is not None:
                    print(
                        f"[dev_run_dask_distributed] child process exited "
                        f"with code {proc.returncode}; tearing down."
                    )
                    shutdown()
                    return proc.returncode or 1
            time.sleep(1.0)
    except KeyboardInterrupt:
        shutdown()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--num-workers",
        type=int,
        default=2,
        help="Number of dask-worker subprocesses (default: 2)",
    )
    parser.add_argument(
        "--scheduler-port",
        type=int,
        default=8786,
        help="Dask scheduler RPC port (default: 8786)",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8787,
        help="Dask scheduler dashboard port (default: 8787)",
    )
    parser.add_argument(
        "--no-celery",
        action="store_true",
        help="Skip Celery worker + beat (useful when only testing indexing)",
    )
    parser.add_argument(
        "--no-indexer",
        action="store_true",
        help="Skip the indexer-scheduler (useful when bringing your own "
        "by running update.py manually with DASK_SCHEDULER_ADDRESS set)",
    )
    args = parser.parse_args()
    return run(
        num_workers=args.num_workers,
        scheduler_port=args.scheduler_port,
        dashboard_port=args.dashboard_port,
        no_celery=args.no_celery,
        no_indexer=args.no_indexer,
    )


if __name__ == "__main__":
    sys.exit(main())
