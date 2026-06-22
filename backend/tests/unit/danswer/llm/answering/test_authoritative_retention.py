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

def test_returns_authoritative_when_none_cited():
    docs = [
        doc("os1", "outsystems"),
        doc("s1", "slack"),            # not protected
        doc("w1", "web"),
    ]
    out = ar.select_authoritative_candidates(docs, already_cited_doc_ids={"s1"})
    # slack cited is fine (not authoritative); both authoritative docs are candidates
    assert [d.document_id for d in out] == ["os1", "w1"]


def test_surfaces_uncited_authoritative_even_when_another_is_cited():
    # Citing one authoritative doc must NOT suppress a different uncited one.
    docs = [doc("os1", "outsystems"), doc("os2", "outsystems"), doc("w1", "web")]
    out = ar.select_authoritative_candidates(docs, already_cited_doc_ids={"os2"})
    assert [d.document_id for d in out] == ["os1", "w1"]  # os2 cited; os1, w1 surfaced


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
    def __init__(self): self.calls = 0
    def invoke(self, prompt):
        self.calls += 1
        raise RuntimeError("llm down")


class _FlakyLLM:
    """Fails the first N invokes, then returns `reply`."""
    def __init__(self, fail_times, reply):
        self._fail_times = fail_times
        self._reply = reply
        self.calls = 0
    def invoke(self, prompt):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient timeout")
        return SimpleNamespace(content=self._reply)


def test_verify_returns_supporting_docs():
    cands = [doc("os1", "outsystems", "Forma"), doc("os2", "outsystems", "Tax")]
    out = ar.verify_supporting_docs("answer", cands, _StubLLM("[1]"))
    assert [d.document_id for d in out] == ["os1"]


def test_verify_fail_closed_after_exhausting_retries():
    cands = [doc("os1", "outsystems")]
    llm = _BoomLLM()
    assert ar.verify_supporting_docs("answer", cands, llm, max_attempts=2) == []
    assert llm.calls == 2  # tried twice, then gave up


def test_verify_retries_then_succeeds_on_transient_failure():
    cands = [doc("os1", "outsystems"), doc("os2", "outsystems")]
    llm = _FlakyLLM(fail_times=1, reply="[2]")  # first call times out, retry works
    out = ar.verify_supporting_docs("answer", cands, llm, max_attempts=2)
    assert [d.document_id for d in out] == ["os2"]
    assert llm.calls == 2


def test_verify_noop_without_candidates_or_answer():
    assert ar.verify_supporting_docs("answer", [], _StubLLM("[1]")) == []
    assert ar.verify_supporting_docs("   ", [doc("os1", "outsystems")], _StubLLM("[1]")) == []


# ---- retained_authoritative_footer (footer block, stub LLM) --------------------

def test_footer_appended_for_verified_doc():
    fcd = [doc("os1", "outsystems", "Forma", link="http://f"), doc("s1", "slack")]
    out = ar.retained_authoritative_footer("ans", fcd, set(), _StubLLM("[1]"))
    assert "Authoritative sources" in out and "[Forma](http://f)" in out


def test_footer_empty_and_no_llm_call_when_authoritative_already_cited():
    fcd = [doc("os1", "outsystems"), doc("s1", "slack")]
    llm = _BoomLLM()  # would raise if the verify call ran
    assert ar.retained_authoritative_footer("ans", fcd, {"os1"}, llm) == ""
    assert llm.calls == 0  # gated out before any LLM call


def test_footer_empty_when_verify_rejects():
    fcd = [doc("os1", "outsystems"), doc("s1", "slack")]
    assert ar.retained_authoritative_footer("ans", fcd, set(), _StubLLM("[]")) == ""
