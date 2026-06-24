"""Unit tests for cap_docs_per_source — caps how many docs any single source
contributes to the final prompt, so a chatty source (e.g. a busy Slack channel)
can't monopolize grounding/citations and drown out curated sources.
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
    monkeypatch.setattr(dp, "MAX_PROMPT_DOCS_PER_SOURCE", 2)


def test_caps_dominant_source_keeping_order() -> None:
    # 5 Slack + 2 OutSystems (already promoted to front) -> Slack capped to 2.
    docs = [
        _doc("os1", "outsystems"),
        _doc("os2", "outsystems"),
        _doc("s1", "slack"),
        _doc("s2", "slack"),
        _doc("s3", "slack"),
        _doc("s4", "slack"),
        _doc("s5", "slack"),
    ]
    out = dp.cap_docs_per_source(docs)
    assert _ids(out) == ["os1", "os2", "s1", "s2"]


def test_caps_each_source_independently() -> None:
    docs = [
        _doc("s1", "slack"),
        _doc("w1", "web"),
        _doc("s2", "slack"),
        _doc("w2", "web"),
        _doc("s3", "slack"),
        _doc("w3", "web"),
    ]
    out = dp.cap_docs_per_source(docs)
    # first 2 of each source, in original order
    assert _ids(out) == ["s1", "w1", "s2", "w2"]


def test_keeps_top_n_by_position() -> None:
    # cap keeps the FIRST N (highest-ranked / promoted), drops the tail
    docs = [_doc(f"s{i}", "slack") for i in range(6)]
    assert _ids(dp.cap_docs_per_source(docs)) == ["s0", "s1"]


def test_under_cap_unchanged() -> None:
    docs = [_doc("s1", "slack"), _doc("os1", "outsystems")]
    assert _ids(dp.cap_docs_per_source(docs)) == ["s1", "os1"]


def test_disabled_when_cap_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dp, "MAX_PROMPT_DOCS_PER_SOURCE", 0)
    docs = [_doc(f"s{i}", "slack") for i in range(5)]
    assert dp.cap_docs_per_source(docs) is docs


def test_handles_enum_like_source_type(monkeypatch: pytest.MonkeyPatch) -> None:
    # source_type may be a DocumentSource enum (has .value) rather than a str.
    monkeypatch.setattr(dp, "MAX_PROMPT_DOCS_PER_SOURCE", 1)
    docs = [
        _doc("a", SimpleNamespace(value="slack")),
        _doc("b", SimpleNamespace(value="slack")),
        _doc("c", SimpleNamespace(value="outsystems")),
    ]
    assert _ids(dp.cap_docs_per_source(docs)) == ["a", "c"]
