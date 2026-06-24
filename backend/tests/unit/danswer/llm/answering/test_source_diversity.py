"""Unit tests for ensure_source_diversity — guarantees curated KB/web docs
aren't crowded out of the final prompt by a chatty high-relevance source.

Promotes up to SOURCE_DIVERSITY_RESERVED_SLOTS of the highest-ranked
protected-source docs to the front, preserving the rest of the order. This is
the replacement for the old two-query source-prioritization hack.
"""
from types import SimpleNamespace

import pytest

from danswer.llm.answering import doc_pruning as dp


def _doc(name: str, source: str) -> SimpleNamespace:
    return SimpleNamespace(semantic_identifier=name, source_type=source)


def _ids(docs: list[SimpleNamespace]) -> list[str]:
    return [d.semantic_identifier for d in docs]


@pytest.fixture(autouse=True)
def _cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dp, "PROTECTED_SOURCES", ["web", "sfkbarticles"])
    monkeypatch.setattr(dp, "SOURCE_DIVERSITY_RESERVED_SLOTS", 2)


def test_promotes_top_protected_docs_to_front() -> None:
    # Slack dominates the top; KB/web are ranked lower and would be cut.
    docs = [
        _doc("s1", "slack"),
        _doc("s2", "slack"),
        _doc("kb1", "sfkbarticles"),
        _doc("s3", "slack"),
        _doc("w1", "web"),
    ]
    out = dp.ensure_source_diversity(docs)
    # top 2 protected hoisted (in their original relative order); rest preserved
    assert _ids(out) == ["kb1", "w1", "s1", "s2", "s3"]


def test_caps_at_reserved_slots() -> None:
    # 3 protected present, but only the top 2 are promoted.
    docs = [
        _doc("s1", "slack"),
        _doc("kb1", "web"),
        _doc("kb2", "web"),
        _doc("kb3", "sfkbarticles"),
    ]
    out = dp.ensure_source_diversity(docs)
    assert _ids(out) == ["kb1", "kb2", "s1", "kb3"]


def test_noop_when_no_protected_docs() -> None:
    docs = [_doc("s1", "slack"), _doc("s2", "slack")]
    assert dp.ensure_source_diversity(docs) is docs


def test_disabled_when_reserved_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dp, "SOURCE_DIVERSITY_RESERVED_SLOTS", 0)
    docs = [_doc("kb1", "web"), _doc("s1", "slack")]
    assert dp.ensure_source_diversity(docs) is docs


def test_already_at_front_unchanged() -> None:
    docs = [_doc("kb1", "web"), _doc("kb2", "web"), _doc("s1", "slack")]
    assert _ids(dp.ensure_source_diversity(docs)) == ["kb1", "kb2", "s1"]
