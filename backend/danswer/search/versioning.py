"""Shared UiPath documentation version logic — the single source of truth for the
Automation-Suite-vs-standalone labeling schemes, used by BOTH the normal answer path
(SearchTool / doc_pruning) and the deep-RAG controller so version handling is identical
across flows (no duplicated per-flow copies).

Schemes:
  - <=24 releases: "YYYY.MM"      e.g. 2024.10
  - >=25 releases: product-dependent — Automation Suite deployment uses "2.NNMM"
    (e.g. 2025.10 -> 2.2510) while standalone/desktop products keep "YYYY.MM" (2025.10).
    The URL's deployment-flavor segment decides (.../automation-suite/2.2510/... vs
    .../standalone/2025.10/...), and a single product (e.g. Orchestrator) carries both.

Because a "25.10"/"2025.10" token is therefore ambiguous by product, we emit BOTH corpus
forms for >=25 and let the data decide (the on-version filter, or a page's indexed
versions) — no product flag needed in code.
"""
import re
from collections.abc import Callable
from typing import Any

# A version token as written in a QUERY/question: "25.10", "2025.10", "24.10", "2024.10"
# (the 2X era). The optional "20" prefix matches both the 2- and 4-digit spellings; groups
# are (NN, MM) with NN the 2-digit year.
VERSION_RE = re.compile(r"\b(?:20)?(2\d)\.(\d{1,2})\b")

# A version token as it appears in the CORPUS (doc links / identifiers): "2.NNMM"
# (Automation Suite, e.g. 2.2510) or "YYYY.MM" (standalone / <=24, e.g. 2025.10, 2024.10).
CORPUS_VERSION_RE = re.compile(r"2\.\d{4}|20\d{2}\.\d{1,2}")


def version_forms(nn: str, mm: str) -> list[str]:
    """Corpus label(s) for a 2X.MM version: <=24 -> [YYYY.MM]; >=25 -> [2.NNMM, YYYY.MM]
    (Automation Suite deployment + standalone). Both are emitted for >=25 because the
    scheme is product-dependent; callers drop whichever isn't actually in the corpus via
    the on-version filter."""
    if int(nn) <= 24:
        return [f"20{nn}.{mm}"]
    return [f"2.{nn}{mm.zfill(2)}", f"20{nn}.{mm}"]


def version_query_variants(query: str) -> list[str]:
    """Rewrite each version token to its corpus form(s) via regex substitution, yielding
    one clean single-form query per variant (deduped). Positional re.sub (not str.replace)
    so every token is rewritten in one pass, in place, without replace-all surprises.
    No-op (returns [query]) when there is no version token."""
    matches = list(VERSION_RE.finditer(query))
    if not matches:
        return [query]
    depth = max(len(version_forms(m.group(1), m.group(2))) for m in matches)
    variants: list[str] = []
    for i in range(depth):

        def repl(m: "re.Match[str]", idx: int = i) -> str:
            fs = version_forms(m.group(1), m.group(2))
            return fs[idx] if idx < len(fs) else fs[-1]

        v = VERSION_RE.sub(repl, query)
        if v not in variants:
            variants.append(v)
    return variants


def corpus_version_tokens(text: str) -> set[str]:
    """The corpus-form version tokens present in a (rewritten) query variant."""
    return set(CORPUS_VERSION_RE.findall(text))


def question_version_candidates(question: str | None) -> list[str]:
    """Corpus-form version candidate(s) for a version named in the QUESTION, or [] when
    zero or more than one DISTINCT version is mentioned (ambiguous — don't guess). Scheme-
    aware: '25.10'/'2025.10' -> ['2.2510', '2025.10'] (Automation Suite + standalone),
    '23.10' -> ['2023.10']. Used to relabel/target the asked-about version across BOTH
    schemes (the caller picks whichever candidate the page actually has indexed)."""
    seen: set[tuple[str, str]] = set()
    for m in VERSION_RE.finditer(question or ""):
        seen.add((m.group(1), str(int(m.group(2)))))  # (NN, minor without leading zero)
    if len(seen) != 1:
        return []
    nn, mm = next(iter(seen))
    return version_forms(nn, mm)


def section_matches_version(section: Any, tokens: set[str]) -> bool:
    """True if the section references any of `tokens` in its link or identifier — used to
    drop off-version hits from a version-substituted query variant."""
    cc = getattr(section, "center_chunk", None) or section
    links = getattr(cc, "source_links", None) or {}
    try:
        link = links.get(0) or links.get("0") or ""
    except Exception:
        link = ""
    haystack = f"{link} {getattr(section, 'semantic_identifier', '') or ''}"
    return any(tok in haystack for tok in tokens)


def version_merged_sections(
    retrieve_one: Callable[[str], list[Any]], query: str
) -> list[Any]:
    """Version-normalized retrieval, shared by the normal path (SearchTool) and deep-RAG
    (per-sub-question). For each corpus-form variant of `query`, call `retrieve_one(variant)`,
    keep only ON-VERSION hits from that variant (so the form absent from this doc-set drops
    its off-version noise), merge deduped by (document_id, chunk_id), and sort by score desc.
    A query with no version token runs a single `retrieve_one(query)` unchanged.

    Raw-query fallback: when the on-version filter drops EVERYTHING (the named version has
    no corpus presence at all — e.g. a question about a future/unreleased version), fall
    back to a plain `retrieve_one(query)` so callers never get an empty result where an
    unfiltered search would have found something (the best-available docs still beat
    nothing; without this, deep-RAG sub-questions about unreleased versions collated zero
    docs while SearchTool had its own private fallback)."""
    variants = version_query_variants(query)
    seen: set[tuple[Any, Any]] = set()
    merged: list[Any] = []
    filtered = False  # did the on-version filter actually apply anywhere?
    for variant in variants:
        tokens = corpus_version_tokens(variant)
        filtered = filtered or bool(tokens)
        for s in retrieve_one(variant):
            if tokens and not section_matches_version(s, tokens):
                continue
            key = (getattr(s, "document_id", None), getattr(s, "chunk_id", None))
            if key not in seen:
                seen.add(key)
                merged.append(s)
    if not merged and filtered:
        # every variant's hits were off-version -> unfiltered raw-query retrieval
        merged = list(retrieve_one(query))
    merged.sort(key=lambda s: (getattr(s, "score", 0.0) or 0.0), reverse=True)
    return merged
