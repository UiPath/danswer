"""Unit tests for _query_vespa's two paths.

When prioritize_sources is False (the reranking path) it issues a SINGLE
all-sources Vespa query, so scores stay on one comparable normalize_linear
scale for the cross-encoder. When True (reranking off) it keeps the legacy
two-query flow: an all-sources query plus a second source-filtered query whose
hits are concatenated. The second query independently normalizes a narrower
set, which is why we skip it under reranking.
"""
import pytest

from danswer.document_index.vespa import index as vespa_index


@pytest.fixture
def count_vespa_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace the network call with a recorder that returns no hits, so the
    function exercises its branching without touching Vespa."""
    calls: list[dict] = []

    def fake_helper(params: dict) -> list:
        calls.append(dict(params))
        return []

    monkeypatch.setattr(vespa_index, "query_vespa_helper", fake_helper)
    return calls


def test_single_query_when_not_prioritizing(count_vespa_calls: list[dict]) -> None:
    out = vespa_index._query_vespa(
        {"yql": "select * from sources x where true", "query": "hello"},
        prioritize_sources=False,
    )
    assert len(count_vespa_calls) == 1  # one all-sources query, no second query
    assert out == []
    # the single query must not have a source_type filter spliced into the YQL
    assert "source_type contains" not in count_vespa_calls[0]["yql"]


def test_two_queries_when_prioritizing(count_vespa_calls: list[dict]) -> None:
    vespa_index._query_vespa(
        {
            "yql": "select * from sources x where true",
            "query": "hello",
            "prioritized_sources": ["web"],
        },
        prioritize_sources=True,
    )
    assert len(count_vespa_calls) == 2  # all-sources + prioritized-sources
    # the second (prioritized) query appends the source filter to the YQL
    assert "source_type contains" not in count_vespa_calls[0]["yql"]
    assert 'source_type contains "web"' in count_vespa_calls[1]["yql"]


def test_default_keeps_legacy_two_query_behavior(count_vespa_calls: list[dict]) -> None:
    # No explicit flag => prioritize_sources defaults True => legacy behavior,
    # so callers other than the reranking path are unaffected.
    vespa_index._query_vespa(
        {"yql": "select * from sources x where true", "query": "hello"}
    )
    assert len(count_vespa_calls) == 2
