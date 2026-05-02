"""Run the DB retention policies once, manually.

Useful for the first cleanup against a long-accumulated backlog (e.g.
the kombu_queue_message bloat the Celery beat-vs-dead-worker race
created), without waiting for the daily 08:00 UTC beat tick.

Usage:

    cd backend
    PYTHONPATH=$(pwd) python scripts/cleanup_stale_db.py            # run all policies
    PYTHONPATH=$(pwd) python scripts/cleanup_stale_db.py --dry-run  # preview row counts
    PYTHONPATH=$(pwd) python scripts/cleanup_stale_db.py --policy=kombu_message
    PYTHONPATH=$(pwd) python scripts/cleanup_stale_db.py --policy=chat,index_attempt

Honors the same RETENTION_DAYS_* env vars as the periodic task. To
override for one run without editing your shell profile:

    RETENTION_DAYS_KOMBU=3 python scripts/cleanup_stale_db.py --policy=kombu_message
"""
from __future__ import annotations

import argparse
import sys

from danswer.db.retention import RETENTION_POLICIES
from danswer.db.retention import run_retention_policies


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the row counts that would be deleted; rolls back instead "
        "of committing.",
    )
    parser.add_argument(
        "--policy",
        type=str,
        default=None,
        help="Comma-separated subset of policy names to run. Available: "
        + ", ".join(RETENTION_POLICIES.keys()),
    )
    args = parser.parse_args()

    only: list[str] | None = None
    if args.policy:
        only = [p.strip() for p in args.policy.split(",") if p.strip()]
        unknown = set(only) - set(RETENTION_POLICIES.keys())
        if unknown:
            print(
                f"Unknown policy/policies: {sorted(unknown)}. "
                f"Available: {sorted(RETENTION_POLICIES.keys())}",
                file=sys.stderr,
            )
            return 2

    label = "DRY RUN" if args.dry_run else "EXECUTE"
    scope = ",".join(only) if only else "all"
    print(f"[{label}] Running retention policies: {scope}")

    results = run_retention_policies(dry_run=args.dry_run, only=only)

    if not results:
        print("(no policies ran — see logs)")
        return 0

    width = max(len(k) for k in results.keys())
    print()
    print(f"{'policy':<{width}}  rows {'(would delete)' if args.dry_run else 'deleted'}")
    print(f"{'-' * width}  -----")
    for name, n in sorted(results.items(), key=lambda kv: -kv[1]):
        print(f"{name:<{width}}  {n}")
    print()
    if args.dry_run:
        print("No changes committed. Re-run without --dry-run to actually delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
