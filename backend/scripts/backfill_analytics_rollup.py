"""Populate `analytics_daily_rollup` AND `analytics_user_first_seen` from
existing chat data.

Run this ONCE after deploying the rollup feature, before the next chat
retention sweep deletes any old data. After this completes, the daily
Celery beat task (`run_analytics_rollup_task`) keeps both tables fresh.
Walking history ascending means each user's first_seen_date is their true
first-ever active day (within the data that still exists).

Usage:

    cd backend
    PYTHONPATH=$(pwd) python scripts/backfill_analytics_rollup.py
    PYTHONPATH=$(pwd) python scripts/backfill_analytics_rollup.py --start=2024-01-01
    PYTHONPATH=$(pwd) python scripts/backfill_analytics_rollup.py --start=2024-01-01 --end=2024-12-31

Defaults:
  --start: earliest `chat_session.time_created` (or today if no chats).
  --end:   today (UTC).

Idempotent — re-running re-upserts the same dates with the same values.
"""
from __future__ import annotations

import argparse
import datetime
import sys

from sqlalchemy.orm import Session

from danswer.db.analytics_rollup import backfill_all_rollups
from danswer.db.analytics_rollup import get_earliest_chat_date
from danswer.db.engine import get_sqlalchemy_engine


def _parse_date(s: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid date {s!r}; expected YYYY-MM-DD"
        ) from e


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start",
        type=_parse_date,
        default=None,
        help="Start date YYYY-MM-DD. Defaults to earliest chat_session.",
    )
    parser.add_argument(
        "--end",
        type=_parse_date,
        default=None,
        help="End date YYYY-MM-DD (inclusive). Defaults to today (UTC).",
    )
    args = parser.parse_args()

    engine = get_sqlalchemy_engine()
    today = datetime.datetime.now(tz=datetime.timezone.utc).date()
    end = args.end or today

    if args.start:
        start = args.start
    else:
        with Session(engine) as db_session:
            earliest = get_earliest_chat_date(db_session)
        if earliest is None:
            print(
                "No chat_session rows yet — nothing to backfill. The daily "
                "Celery task will start populating from now on.",
                file=sys.stderr,
            )
            return 0
        start = earliest

    if start > end:
        print(
            f"--start {start} is after --end {end}; nothing to do.",
            file=sys.stderr,
        )
        return 2

    span_days = (end - start).days + 1
    print(
        f"Backfilling analytics_daily_rollup for {span_days} day(s) "
        f"({start} → {end})…"
    )
    n = backfill_all_rollups(start, end)
    print(f"Done. {n} day(s) upserted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
