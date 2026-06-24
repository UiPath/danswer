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

Per-source overrides
--------------------
The global cap above is uniform: raising it lifts the cap for *every*
source, including ones with real external rate limits or a shared
credential (Slack, Jira, Confluence). When you only want to parallelize a
*specific* source — most commonly `web`, where every cc-pair has its own
dummy credential and mostly distinct domains — set a per-source override
instead. Comma-separated `source=cap` pairs; `source` is the
`DocumentSource` value (lowercase, e.g. `web`, `slack`); `cap` follows the
same convention as the global (0 = uncapped):

    # web runs uncapped (bounded only by NUM_INDEXING_WORKERS + the
    # per-cc-pair lock); everything else stays at the global default of 1.
    export INDEXING_PER_SOURCE_CAP_OVERRIDES="web=0"

    # web up to 3 at a time, slack still 1 (explicit):
    export INDEXING_PER_SOURCE_CAP_OVERRIDES="web=3,slack=1"

A source not named in the override map falls back to `PER_SOURCE_CAP`.
Malformed entries are skipped (the source keeps the global default).
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


def _resolve_overrides() -> dict[str, int]:
    """Parse `INDEXING_PER_SOURCE_CAP_OVERRIDES` into {source: cap}.

    Format: comma-separated `source=cap` pairs, e.g. `web=0,slack=1`.
    Source keys are lowercased to match `DocumentSource(...).value`.
    Malformed pairs are ignored so one typo can't wipe the whole map.
    """
    raw = os.environ.get("INDEXING_PER_SOURCE_CAP_OVERRIDES", "").strip()
    overrides: dict[str, int] = {}
    if not raw:
        return overrides
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        source, _, cap_str = part.partition("=")
        source = source.strip().lower()
        if not source:
            continue
        try:
            overrides[source] = max(0, int(cap_str.strip()))
        except ValueError:
            continue
    return overrides


# Concurrent attempts per source type. 1 = at most one indexing attempt
# per `DocumentSource` at a time (the generic rule). 0 = uncapped (skip
# the slot logic entirely; rely solely on the per-cc-pair lock +
# NUM_INDEXING_WORKERS).
PER_SOURCE_CAP: int = _resolve_cap()

# Optional per-source overrides on top of PER_SOURCE_CAP. Keyed by
# `DocumentSource` value (lowercase). A source absent here uses
# PER_SOURCE_CAP. See module docstring for the env format.
PER_SOURCE_CAP_OVERRIDES: dict[str, int] = _resolve_overrides()


def cap_for_source(source: str, default: int, overrides: dict[str, int]) -> int:
    """Resolve the concurrency cap for a single `DocumentSource` value.

    Pure helper (default + overrides passed in) so the scheduler can stay
    testable without reaching into module globals.
    """
    return overrides.get(source, default)
