"""Unit tests for verify-then-retain authoritative citations.

Covers the pure pieces (candidate selection / dedupe / index parsing / footer) and
the verify step with a stub LLM. The retention is additive + deduped + fail-closed.
"""
from types import SimpleNamespace

import pytest

from danswer.llm.answering import authoritative_retention as ar


def doc(doc_id, source, name="Doc", content="c", link="http://x"):
    return SimpleNamespace(
        document_id=doc_id, source_type=source, semantic_identifier=name,
        content=content, link=link,
    )


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(ar, "PROTECTED_SOURCES", ["web", "outsystems"])


# ---- select_authoritative_candidates -------------------------------------------

def test_selects_only_uncited_protected_docs():
    docs = [
        doc("os1", "outsystems"),
        doc("s1", "slack"),            # not protected
        doc("os2", "outsystems"),
        doc("w1", "web"),
    ]
    out = ar.select_authoritative_candidates(docs, already_cited_doc_ids={"os2"})
    assert [d.document_id for d in out] == ["os1", "w1"]  # os2 already cited, slack dropped


def test_dedupes_same_document_id():
    docs = [doc("os1", "outsystems"), doc("os1", "outsystems"), doc("os2", "outsystems")]
    out = ar.select_authoritative_candidates(docs, already_cited_doc_ids=set())
    assert [d.document_id for d in out] == ["os1", "os2"]


def test_skips_docs_without_link():
    docs = [doc("os1", "outsystems", link=None), doc("os2", "outsystems", link="http://y")]
    out = ar.select_authoritative_candidates(docs, already_cited_doc_ids=set())
    assert [d.document_id for d in out] == ["os2"]


def test_handles_enum_source_type():
    docs = [doc("os1", SimpleNamespace(value="outsystems"))]
    out = ar.select_authoritative_candidates(docs, already_cited_doc_ids=set())
    assert [d.document_id for d in out] == ["os1"]


# ---- parse_supporting_indices --------------------------------------------------

@pytest.mark.parametrize("raw,n,expected", [
    ("[1, 3]", 3, [0, 2]),
    ("sure: [2]", 3, [1]),
    ("[]", 3, []),
    ("none", 3, []),
    ("[1, 9]", 3, [0]),       # out-of-range dropped
    ("[1, 1]", 3, [0, 0]),    # parser doesn't dedupe ids (caller controls candidates)
])
def test_parse_supporting_indices(raw, n, expected):
    assert ar.parse_supporting_indices(raw, n) == expected


# ---- verify_supporting_docs (stub LLM) -----------------------------------------

class _StubLLM:
    def __init__(self, reply): self._reply = reply
    def invoke(self, prompt): return SimpleNamespace(content=self._reply)


class _BoomLLM:
    def invoke(self, prompt): raise RuntimeError("llm down")


def test_verify_returns_supporting_docs():
    cands = [doc("os1", "outsystems", "Forma"), doc("os2", "outsystems", "Tax")]
    out = ar.verify_supporting_docs("answer", cands, _StubLLM("[1]"))
    assert [d.document_id for d in out] == ["os1"]


def test_verify_fail_closed_on_llm_error():
    cands = [doc("os1", "outsystems")]
    assert ar.verify_supporting_docs("answer", cands, _BoomLLM()) == []


def test_verify_noop_without_candidates_or_answer():
    assert ar.verify_supporting_docs("answer", [], _StubLLM("[1]")) == []
    assert ar.verify_supporting_docs("   ", [doc("os1", "outsystems")], _StubLLM("[1]")) == []


# ---- footer --------------------------------------------------------------------

def test_footer_lists_links_and_is_empty_when_none():
    assert ar.build_authoritative_footer([]) == ""
    out = ar.build_authoritative_footer([doc("os1", "outsystems", "Forma", link="http://f")])
    assert "Authoritative sources" in out and "[Forma](http://f)" in out
