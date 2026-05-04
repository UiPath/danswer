"""Per-source-type indexing concurrency cap.

Generic rule: when `NUM_INDEXING_WORKERS > 1`, only `INDEXING_PER_SOURCE_CAP`
indexing attempts run per `DocumentSource` at a time (default 1). So 4
workers + 4 different source types (e.g. Slack + Github + Confluence +
Jira) means 4× speedup. 4 workers + 4 GitHub cc-pairs (same source type)
means 1 runs and the other 3 stay NOT_STARTED until the running one
finishes; the next 10s scheduler tick reconsiders them.

Adding a new connector requires nothing here — every `DocumentSource`
gets its own slot pool automatically. Connectors that *share* an external
credential (e.g. github + github_files share a PAT) are not currently
collapsed into a single bucket; if you find that distinction matters in
practice, fold them into a single `DocumentSource` rather than reintroduce
a grouping layer.

The cap is enforced *upstream* in the scheduler — see
`background/update.py::kickoff_indexing_jobs`. Over-cap NOT_STARTED rows
are simply not submitted to Dask on a given tick; the next tick picks
them up once a slot frees. Workers don't see the cap at all — there's no
fail-fast or `skipped_*` error_msg associated with this cap.

To disable capping entirely (e.g. you genuinely want N parallel indexers
of the same source type because each cc-pair has its own credential):

    export INDEXING_PER_SOURCE_CAP=0
"""
from __future__ import annotations

import os


def _resolve_cap() -> int:
    raw = os.environ.get("INDEXING_PER_SOURCE_CAP", "").strip()
    if not raw:
        return 1
    try:
        return max(0, int(raw))
    except ValueError:
        return 1


# Concurrent attempts per source type. 1 = at most one indexing attempt
# per `DocumentSource` at a time (the generic rule). 0 = uncapped (skip
# the slot logic entirely; rely solely on the per-cc-pair lock +
# NUM_INDEXING_WORKERS).
PER_SOURCE_CAP: int = _resolve_cap()
