"""Unit tests for the deep_rag controller — the verdict-gated multi-hop orchestration.

The answer generator (`generate` — the reused normal answer flow), retrieval and the
decompose/compare LLM are stubbed, so these assert the ORCHESTRATION: complete -> no
escalation; partial -> escalate; compare-guard keeps single-shot when the deep answer
isn't better; single-shot passes no docs while multi-hop synthesizes over the collated
docs; decomposition is capped; the verdict block is split off (fail-open); and any
deep-path error fails OPEN to the single-shot answer.
"""
from dataclasses import dataclass
from typing import Any
from typing import cast

from langchain_core.messages import AIMessage

from danswer.chat.models import CitationInfo
from danswer.deep_rag.controller import _Gen
from danswer.deep_rag.controller import _split_verdict
from danswer.deep_rag.controller import decompose
from danswer.deep_rag.controller import deep_rag_answer
from danswer.llm.interfaces import LLM


@dataclass
class _Sec:
    document_id: str = "d1"
    chunk_id: int = 0
    semantic_identifier: str = "Doc"
    content: str = "some content"
    combined_content: str = "some content"
    score: float = 0.8


def _retrieve(_query: str, k: int) -> list[_Sec]:
    return [_Sec(document_id=f"d{i}", chunk_id=i) for i in range(k)]


def _gen(
    answer: str = "grounded answer [1]",
    coverage: str = "complete",
    missing: list[str] | None = None,
    citations: list[CitationInfo] | None = None,
) -> _Gen:
    # Default to a grounded (cited), high-confidence single-shot so the "complete" tests
    # aren't downgraded to partial by the escalation signals; tests that want the
    # ungrounded / low-confidence cases pass citations=[] or a low-confidence answer.
    return _Gen(
        answer=answer,
        citations=(
            [CitationInfo(citation_num=1, document_id="d0")]
            if citations is None
            else citations
        ),
        # A real generate() always returns docs (single-shot: its retrieved docs; multi-hop:
        # the collated docs). Non-empty by default so stubs reflect that; the empty-docs
        # fallback guard is exercised by the tests that set docs=[] explicitly.
        docs=[object()],
        coverage=coverage,
        missing=missing or [],
    )


class _FakeGenerate:
    """Stubs the reused normal answer flow. Returns the single-shot `_Gen` when
    `selected_docs is None`, else the multi-hop synthesis `_Gen`. Records every call so
    tests can assert single-shot got no docs and multi-hop got the collated ones."""

    def __init__(self, single_shot: _Gen, multi_hop: _Gen) -> None:
        self.single_shot = single_shot
        self.multi_hop = multi_hop
        self.calls: list[tuple[Any, bool]] = []

    def __call__(
        self, question: str, *, selected_docs: Any = None, want_verdict: bool = False
    ) -> _Gen:
        self.calls.append((selected_docs, want_verdict))
        return self.multi_hop if selected_docs is not None else self.single_shot


class _DecomposeCompareLLM:
    """Only the decompose + compare-guard prompts reach this LLM now (answer generation
    goes through `generate`)."""

    def __init__(
        self,
        subqs: str = "What is A?\nWhat is B?",
        comparison: str = "yes",
        raise_on_decompose: bool = False,
    ) -> None:
        self.subqs = subqs
        self.comparison = comparison
        self.raise_on_decompose = raise_on_decompose
        self.calls: list[str] = []

    def invoke(
        self, prompt: object, tools: object = None, tool_choice: object = None
    ) -> AIMessage:
        p = prompt if isinstance(prompt, str) else str(prompt)
        self.calls.append(p)
        if "refined answer" in p:  # compare-guard prompt
            return AIMessage(content=self.comparison)
        if "list of sub-questions" in p:  # decomposition prompt
            if self.raise_on_decompose:
                raise RuntimeError("decompose boom")
            return AIMessage(content=self.subqs)
        return AIMessage(content="")

    def called_decompose(self) -> bool:
        return any("list of sub-questions" in c for c in self.calls)


# --- gate / escalation orchestration -------------------------------------------------


def test_complete_verdict_does_not_escalate() -> None:
    gen = _FakeGenerate(_gen("A is X", coverage="complete"), _gen("REFINED"))
    llm = _DecomposeCompareLLM()
    res = deep_rag_answer("q", gen, _retrieve, cast(LLM, llm))
    assert res.source == "single_shot"
    assert res.escalated is False
    assert res.answer == "A is X"
    assert llm.called_decompose() is False  # never entered the deep path
    assert gen.calls == [
        (None, True)
    ]  # exactly one single-shot call, verdict requested


def test_force_escalates_even_when_complete() -> None:
    # the manual per-question override: escalate regardless of a "complete" verdict
    gen = _FakeGenerate(_gen("A is X", coverage="complete"), _gen("REFINED"))
    llm = _DecomposeCompareLLM(comparison="yes")
    res = deep_rag_answer("q", gen, _retrieve, cast(LLM, llm), force=True)
    assert res.escalated is True
    assert res.source == "multi_hop"
    assert res.answer == "REFINED"
    assert llm.called_decompose() is True
    # single-shot with no docs + verdict, then multi-hop over collated docs, no verdict
    assert gen.calls[0] == (None, True)
    assert gen.calls[1][1] is False and gen.calls[1][0]  # collated docs, non-empty


def test_partial_escalates_and_keeps_refined_when_better() -> None:
    gen = _FakeGenerate(
        _gen("partial", coverage="partial", missing=["hardware specs"]),
        _gen("REFINED ANSWER"),
    )
    llm = _DecomposeCompareLLM(comparison="yes")
    res = deep_rag_answer("compare A and B", gen, _retrieve, cast(LLM, llm))
    assert res.escalated is True
    assert res.source == "multi_hop"
    assert res.answer == "REFINED ANSWER"
    assert res.sub_questions == ["What is A?", "What is B?"]


def test_partial_with_citations_compares_and_can_keep_single_shot() -> None:
    # a model-partial single-shot that IS grounded (has citations) is a real answer, so we
    # escalate AND ask the judge to compare; if the judge says the deep answer isn't better
    # we keep the single-shot (compare policy, like the duplicated flow).
    gen = _FakeGenerate(
        _gen("partial but cited [1]", coverage="partial"), _gen("REFINED ANSWER")
    )
    llm = _DecomposeCompareLLM(comparison="no")
    res = deep_rag_answer("q", gen, _retrieve, cast(LLM, llm))
    assert res.escalated is True  # we did escalate
    assert res.source == "single_shot"  # ...but the judge kept the (cited) single-shot
    assert llm.called_decompose() is True


def test_no_citations_takes_deep_answer_without_comparing() -> None:
    # an UNGROUNDED single-shot (no citations, e.g. a punt) is superseded by the deep
    # answer outright — the judge is NOT consulted (so a flaky judge can't resurrect it),
    # even when the model self-reported "complete".
    gen = _FakeGenerate(
        _gen("insufficient, check SME", coverage="complete", citations=[]),
        _gen("REFINED ANSWER"),
    )
    llm = _DecomposeCompareLLM(comparison="no")  # would reject if consulted
    res = deep_rag_answer("q", gen, _retrieve, cast(LLM, llm))
    assert res.escalated is True
    assert res.source == "multi_hop"  # deep answer kept despite the "no" verdict
    assert res.answer == "REFINED ANSWER"


def test_low_confidence_escalates_and_compares() -> None:
    # confidence < threshold on a grounded answer -> escalate AND compare (not a punt);
    # here the judge says the deep answer isn't better, so we keep the single-shot.
    gen = _FakeGenerate(
        _gen("Answer. [Confidence Score]: 60", coverage="complete"),
        _gen("REFINED ANSWER"),
    )
    llm = _DecomposeCompareLLM(comparison="no")
    res = deep_rag_answer("q", gen, _retrieve, cast(LLM, llm))
    assert res.escalated is True  # low confidence forced escalation
    assert (
        res.source == "single_shot"
    )  # ...and the judge WAS consulted (kept single-shot)


def test_high_confidence_complete_does_not_escalate() -> None:
    gen = _FakeGenerate(
        _gen("Answer. [Confidence Score]: 95", coverage="complete"),
        _gen("REFINED ANSWER"),
    )
    llm = _DecomposeCompareLLM()
    res = deep_rag_answer("q", gen, _retrieve, cast(LLM, llm))
    assert res.escalated is False
    assert res.source == "single_shot"
    assert llm.called_decompose() is False


def test_below_confidence_threshold_parsing() -> None:
    from danswer.deep_rag.controller import _below_confidence_threshold as low

    assert low("... [Confidence Score]: 60") is True
    assert low("Confidence: 79%") is True
    assert low("[Confidence Score]: 80") is True  # boundary: <= 80 escalates
    assert low("[Confidence Score]: 81") is False  # 81 is above the <= 80 threshold
    assert low("[Confidence Score]: 85") is False
    assert low("confidence score of 95") is False
    assert low("no score mentioned here") is False
    assert low("Automation Suite 2024.10 shipped") is False  # no 'confidence' token


def test_forced_on_complete_still_falls_back_when_not_better() -> None:
    # single-shot was COMPLETE but the user forced deep; the compare-guard still applies
    # and keeps single-shot when the deep answer isn't substantially better.
    gen = _FakeGenerate(_gen("good enough", coverage="complete"), _gen("REFINED"))
    llm = _DecomposeCompareLLM(comparison="no")
    res = deep_rag_answer("q", gen, _retrieve, cast(LLM, llm), force=True)
    assert res.escalated is True
    assert res.source == "single_shot"
    assert res.answer == "good enough"


def test_deep_path_error_fails_open_to_single_shot() -> None:
    gen = _FakeGenerate(_gen("safe fallback", coverage="partial"), _gen("REFINED"))
    llm = _DecomposeCompareLLM(raise_on_decompose=True)
    res = deep_rag_answer("q", gen, _retrieve, cast(LLM, llm))
    assert res.source == "single_shot"
    assert res.escalated is True
    assert res.answer == "safe fallback"


def test_result_carries_citations_and_docs_from_generate() -> None:
    # the normal-flow citations + docs must propagate to the result (the caller surfaces
    # sources + resolves citations from these, not by re-parsing the answer)
    mh = _Gen(
        answer="deep [1]",
        citations=[CitationInfo(citation_num=1, document_id="dX")],
        docs=[object()],
        coverage="complete",
        missing=[],
    )
    gen = _FakeGenerate(_gen("p", coverage="partial"), mh)
    res = deep_rag_answer("q", gen, _retrieve, cast(LLM, _DecomposeCompareLLM()))
    assert res.source == "multi_hop"
    assert [c.document_id for c in res.citations] == ["dX"]
    assert len(res.docs) == 1


def test_generate_empty_selected_docs_keeps_freshly_retrieved_docs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Regression: when collate curates NOTHING, generate is called with selected_docs=[].
    # SearchTool truthy-gates the specified-sections path (`if self.selected_docs:`), so an
    # empty list falls back to a NORMAL retrieval whose answer DOES carry top_documents +
    # citations. The gen.docs override must be skipped there (truthy check, not `is not
    # None`) — overwriting with the empty list stripped the docs off a real cited answer,
    # so Slack saw 0 docs and went silent. Also assert: None -> fresh docs; non-empty ->
    # the curated sections are surfaced.
    import types

    import danswer.deep_rag.controller as ctrl

    fresh = ["FRESH"]

    def fake_get_search_answer(**kwargs: Any) -> Any:
        return types.SimpleNamespace(
            answer="grounded [1]",
            citations=[CitationInfo(citation_num=1, document_id="d0")],
            docs=types.SimpleNamespace(top_documents=list(fresh)),
        )

    monkeypatch.setattr(ctrl, "get_search_answer", fake_get_search_answer)
    monkeypatch.setattr(ctrl, "llm_doc_from_inference_section", lambda s: s)
    monkeypatch.setattr(
        ctrl,
        "chunks_or_sections_to_search_docs",
        lambda secs: [("CURATED", getattr(s, "document_id", None)) for s in secs],
    )
    generate = ctrl._make_generate(
        cast(Any, types.SimpleNamespace(id=1)), None, cast(Any, None), "", True, False
    )

    # empty curated list -> fresh retrieval ran; keep its docs (was [] before the fix)
    assert generate("q", selected_docs=[], want_verdict=False).docs == fresh
    # single-shot (None) -> fresh docs too
    assert generate("q", selected_docs=None, want_verdict=False).docs == fresh
    # non-empty curated list -> surface exactly those sections
    assert generate(
        "q", selected_docs=[_Sec(document_id="dX")], want_verdict=False
    ).docs == [("CURATED", "dX")]


def test_generate_external_verdict_judge(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The coverage gate is a SEPARATE judge call over question+answer (the old in-answer
    # suffix flattened the answer's markdown). Verify: judge "partial" -> coverage partial
    # + missing; judge crash -> fail-OPEN complete; no judge llm -> complete, no call.
    import types

    import danswer.deep_rag.controller as ctrl

    def fake_get_search_answer(**kwargs: Any) -> Any:
        return types.SimpleNamespace(
            answer="grounded [1]",
            citations=[CitationInfo(citation_num=1, document_id="d0")],
            docs=types.SimpleNamespace(top_documents=["D"]),
        )

    monkeypatch.setattr(ctrl, "get_search_answer", fake_get_search_answer)

    class _JudgeLLM:
        def __init__(self, out: str | None) -> None:
            self.out, self.calls = out, 0

        def invoke(self, prompt: str) -> Any:
            self.calls += 1
            if self.out is None:
                raise RuntimeError("judge down")
            return AIMessage(content=self.out)

    persona = cast(Any, types.SimpleNamespace(id=1))

    judge = _JudgeLLM('---VERDICT---\n{"coverage": "partial", "missing": ["hardware"]}')
    gen = ctrl._make_generate(
        persona, None, cast(Any, None), "", True, False, cast(Any, judge)
    )
    g = gen("q", selected_docs=None, want_verdict=True)
    assert (g.coverage, g.missing, judge.calls) == ("partial", ["hardware"], 1)
    assert g.answer == "grounded [1]"  # answer untouched by the gate

    # bare JSON without the marker also parses
    judge = _JudgeLLM('{"coverage": "partial", "missing": []}')
    gen = ctrl._make_generate(
        persona, None, cast(Any, None), "", True, False, cast(Any, judge)
    )
    assert gen("q", selected_docs=None, want_verdict=True).coverage == "partial"

    # judge failure -> fail-open complete
    judge = _JudgeLLM(None)
    gen = ctrl._make_generate(
        persona, None, cast(Any, None), "", True, False, cast(Any, judge)
    )
    assert gen("q", selected_docs=None, want_verdict=True).coverage == "complete"

    # want_verdict=False -> judge never called
    judge = _JudgeLLM('---VERDICT---\n{"coverage": "partial", "missing": []}')
    gen = ctrl._make_generate(
        persona, None, cast(Any, None), "", True, False, cast(Any, judge)
    )
    assert gen("q", selected_docs=None, want_verdict=False).coverage == "complete"
    assert judge.calls == 0


def test_empty_multi_hop_falls_back_to_single_shot_when_single_shot_grounded() -> None:
    # deep answer came back with no text; the single-shot IS grounded (has citations) ->
    # keep the real cited single-shot over the empty deep one.
    ss = _gen(
        "real single-shot [1]", coverage="partial"
    )  # grounded + partial -> escalate
    mh = _Gen(
        answer="   ", citations=[], docs=[object()], coverage="complete", missing=[]
    )
    res = deep_rag_answer(
        "q", _FakeGenerate(ss, mh), _retrieve, cast(LLM, _DecomposeCompareLLM())
    )
    assert res.source == "single_shot"
    assert res.escalated is True


def test_no_docs_multi_hop_falls_back_only_if_single_shot_is_grounded() -> None:
    # empty-DOCS multi-hop (would render as silence on Slack). Fall back to the single-shot
    # ONLY when it is grounded; here it IS -> single-shot wins.
    ss = _gen("cited single-shot [1]", coverage="partial")  # grounded
    mh = _Gen(
        answer="deep answer [1]",
        citations=[CitationInfo(citation_num=1, document_id="dX")],
        docs=[],  # no docs
        coverage="complete",
        missing=[],
    )
    res = deep_rag_answer(
        "q", _FakeGenerate(ss, mh), _retrieve, cast(LLM, _DecomposeCompareLLM())
    )
    assert res.source == "single_shot"


def test_neither_grounded_surfaces_single_shot_punt() -> None:
    # WINNING REQUIRES GROUNDING: if NEITHER answer is grounded (single-shot uncited; deep
    # answer's citations have no backing docs), no one wins -> surface the single-shot's
    # honest punt, never the ungrounded deep answer.
    ss = _gen(
        "ungrounded punt", citations=[]
    )  # no citations -> escalate via no_citations
    mh = _Gen(
        answer="deep answer [1]",
        citations=[CitationInfo(citation_num=1, document_id="dX")],
        docs=[],  # citations but NO docs -> not grounded (would be silence on Slack)
        coverage="complete",
        missing=[],
    )
    res = deep_rag_answer(
        "q", _FakeGenerate(ss, mh), _retrieve, cast(LLM, _DecomposeCompareLLM())
    )
    assert res.source == "single_shot"


# --- verdict split (fail-open) -------------------------------------------------------


def test_split_verdict_parses_trailing_block() -> None:
    clean, coverage, missing = _split_verdict(
        'The answer [1].\n---VERDICT---\n{"coverage": "partial", "missing": ["x", "y"]}'
    )
    assert clean == "The answer [1]."
    assert coverage == "partial"
    assert missing == ["x", "y"]


def test_split_verdict_missing_block_fails_open_complete() -> None:
    clean, coverage, missing = _split_verdict("Just an answer, no verdict.")
    assert clean == "Just an answer, no verdict."
    assert coverage == "complete"
    assert missing == []


def test_split_verdict_malformed_json_fails_open_complete() -> None:
    clean, coverage, missing = _split_verdict("Answer.\n---VERDICT---\nnot json")
    assert clean == "Answer."
    assert coverage == "complete"
    assert missing == []


def test_split_verdict_unknown_coverage_value_fails_open_complete() -> None:
    clean, coverage, _ = _split_verdict('Answer.\n---VERDICT---\n{"coverage": "maybe"}')
    assert coverage == "complete"


# --- decomposition -------------------------------------------------------------------


def test_decompose_respects_cap() -> None:
    llm = _DecomposeCompareLLM(subqs="a?\nb?\nc?\nd?\ne?")
    subqs = decompose("q", ["x"], cast(LLM, llm), max_subquestions=3)
    assert len(subqs) == 3


def test_decompose_falls_back_to_question_when_empty() -> None:
    llm = _DecomposeCompareLLM(subqs="no question marks here")
    subqs = decompose("original q", [], cast(LLM, llm), max_subquestions=3)
    assert subqs == ["original q"]


def test_history_threaded_into_decomposition() -> None:
    gen = _FakeGenerate(_gen("p", coverage="partial"), _gen("REFINED"))
    llm = _DecomposeCompareLLM()
    deep_rag_answer(
        "q", gen, _retrieve, cast(LLM, llm), history="User: earlier about pepsico"
    )
    # the history must reach the decompose prompt so follow-ups resolve in context
    assert any(
        "earlier about pepsico" in c and "list of sub-questions" in c for c in llm.calls
    )


def test_empty_history_renders_no_block() -> None:
    gen = _FakeGenerate(_gen("p", coverage="partial"), _gen("REFINED"))
    llm = _DecomposeCompareLLM()
    deep_rag_answer("q", gen, _retrieve, cast(LLM, llm), history="")
    assert not any("Conversation so far" in c for c in llm.calls)


# --- retrieval curation (unchanged helpers) ------------------------------------------


def test_topk_reserves_protected_sources(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import danswer.deep_rag.controller as ctrl
    from danswer.configs.constants import DocumentSource

    @dataclass
    class _SrcSec:
        document_id: str
        score: float
        source_type: object

    monkeypatch.setattr(ctrl, "SOURCE_DIVERSITY_RESERVED_SLOTS", 2)
    monkeypatch.setattr(ctrl, "_PROTECTED_SET", {DocumentSource.WEB})
    secs = [
        _SrcSec("a", 0.90, DocumentSource.SLACK),
        _SrcSec("b", 0.85, DocumentSource.SLACK),
        _SrcSec("c", 0.80, DocumentSource.SLACK),
        _SrcSec("d", 0.70, DocumentSource.SLACK),
        _SrcSec("e", 0.60, DocumentSource.SLACK),
        _SrcSec("f", 0.55, DocumentSource.SLACK),
        _SrcSec("g", 0.40, DocumentSource.WEB),  # protected, low score
        _SrcSec("h", 0.30, DocumentSource.WEB),  # protected, low score
    ]
    ids = {s.document_id for s in ctrl._topk_reserving_protected(secs, 6)}
    assert len(ids) == 6
    assert "g" in ids and "h" in ids  # protected survive the cut despite low score
    assert "a" in ids and "b" in ids  # top non-protected still kept


def test_topk_plain_when_no_protected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import danswer.deep_rag.controller as ctrl
    from danswer.configs.constants import DocumentSource

    @dataclass
    class _SrcSec:
        document_id: str
        score: float
        source_type: object

    monkeypatch.setattr(ctrl, "SOURCE_DIVERSITY_RESERVED_SLOTS", 2)
    monkeypatch.setattr(ctrl, "_PROTECTED_SET", {DocumentSource.WEB})
    secs = [_SrcSec(chr(97 + i), 1.0 - i * 0.1, DocumentSource.SLACK) for i in range(8)]
    out = [s.document_id for s in ctrl._topk_reserving_protected(secs, 6)]
    assert out == ["a", "b", "c", "d", "e", "f"]


def test_cap_protected_docs_keeps_top_n_distinct_docs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import danswer.deep_rag.controller as ctrl
    from danswer.configs.constants import DocumentSource

    @dataclass
    class _SrcSec:
        document_id: str
        score: float
        source_type: object

    monkeypatch.setattr(ctrl, "_PROTECTED_SET", {DocumentSource.WEB})
    secs = [
        _SrcSec("n1", 0.90, DocumentSource.SLACK),  # non-protected
        _SrcSec("pA", 0.85, DocumentSource.WEB),  # protected doc A (best)
        _SrcSec("pA", 0.60, DocumentSource.WEB),  # doc A, 2nd chunk (same doc kept)
        _SrcSec("pB", 0.80, DocumentSource.WEB),  # protected doc B (2nd best)
        _SrcSec("pC", 0.50, DocumentSource.WEB),  # protected doc C -> dropped (max=2)
        _SrcSec("n2", 0.30, DocumentSource.SLACK),  # non-protected
    ]
    ids = [s.document_id for s in ctrl._cap_protected_docs(secs, 2)]
    # all non-protected kept; top-2 protected DOCUMENTS (A,B) kept incl. both A chunks;
    # 3rd protected doc dropped; input order preserved
    assert ids == ["n1", "pA", "pA", "pB", "n2"]


def testversion_query_variants() -> None:
    from danswer.search.versioning import version_query_variants

    # <=24.MM -> YYYY.MM only, replaced in place
    assert version_query_variants("features in 24.10") == ["features in 2024.10"]
    # >=25.MM -> BOTH corpus forms: 2.NNMM (Automation Suite deployment) + YYYY.MM
    # (standalone/desktop). The retriever's per-variant on-version filter drops whichever
    # form doesn't exist in the doc-set, so emitting both is safe.
    assert version_query_variants("features in 25.10") == [
        "features in 2.2510",
        "features in 2025.10",
    ]
    # multi-version: each token rewritten per variant; 24.10 reuses its only form
    assert version_query_variants("diff 25.10 vs 24.10") == [
        "diff 2.2510 vs 2024.10",
        "diff 2025.10 vs 2024.10",
    ]
    # 4-digit spelling of a >=25 version still expands to both forms
    assert version_query_variants("features in 2025.10") == [
        "features in 2.2510",
        "features in 2025.10",
    ]
    # 4-digit <=24 is already the corpus form -> single, unchanged
    assert version_query_variants("features in 2024.10") == ["features in 2024.10"]
    # mixed 4-digit + 2-digit in one query, rewritten positionally in one pass
    assert version_query_variants("diff 2025.10 vs 24.10") == [
        "diff 2.2510 vs 2024.10",
        "diff 2025.10 vs 2024.10",
    ]
    # no version token -> single unchanged query
    assert version_query_variants("how to create a folder") == [
        "how to create a folder"
    ]
    # minor zero-padded in the 2.NNMM form
    assert version_query_variants("suite 25.4")[0] == "suite 2.2504"


def test_section_version_filter_keeps_only_on_version_hits() -> None:
    # The retriever's per-variant filter: the 2.2510 variant keeps only AS-deployment docs,
    # the 2025.10 variant keeps only standalone docs, and off-version noise (2022.10) is
    # dropped by both — so both schemes coexist without product/assistant knowledge.
    from danswer.search.versioning import corpus_version_tokens
    from danswer.search.versioning import section_matches_version

    assert corpus_version_tokens("features of 2.2510") == {"2.2510"}
    assert corpus_version_tokens("features of 2025.10") == {"2025.10"}
    assert corpus_version_tokens("diff 2.2510 vs 2024.10") == {"2.2510", "2024.10"}
    assert corpus_version_tokens("how to create a folder") == set()

    @dataclass
    class _S:
        semantic_identifier: str
        source_links: dict

    as_doc = _S(
        "Automation Suite - 2.2510.2",
        {
            0: "https://docs.uipath.com/orchestrator/automation-suite/2.2510/release-notes/2-2510-0"
        },
    )
    standalone = _S(
        "Orchestrator - 2025.10.0",
        {
            0: "https://docs.uipath.com/orchestrator/standalone/2025.10/release-notes/2025-10-0"
        },
    )
    noise = _S(
        "Automation Suite - 2022.10.15",
        {
            0: "https://docs.uipath.com/automation-suite/automation-suite/2022.10/release-notes/x"
        },
    )
    # 2.2510 variant: keep AS-deployment doc only
    assert section_matches_version(as_doc, {"2.2510"}) is True
    assert section_matches_version(standalone, {"2.2510"}) is False
    assert section_matches_version(noise, {"2.2510"}) is False
    # 2025.10 variant: keep standalone doc only
    assert section_matches_version(standalone, {"2025.10"}) is True
    assert section_matches_version(as_doc, {"2025.10"}) is False
    assert section_matches_version(noise, {"2025.10"}) is False


def test_strip_judge_noise_removes_sme_and_confidence() -> None:
    # the compare-guard must judge answer substance only, not the SME/confidence footer
    from danswer.deep_rag.controller import _strip_judge_noise

    out = _strip_judge_noise(
        "2024.10 adds Disaster Recovery and RHEL 9.6 support [1].\n\n"
        "[SME]: The Product Manager (Quentin or Omkar)\n"
        "[Confidence Score]: 50"
    )
    assert "Disaster Recovery" in out
    assert "SME" not in out and "Confidence" not in out and "50" not in out
    # markdown-bold footer variant also stripped
    md = _strip_judge_noise("Real content.\n**[SME]**: x\n**[Confidence Score]**: 90")
    assert md == "Real content."
    # a pure punt reduces to empty (its content IS the boilerplate)
    assert (
        _strip_judge_noise(
            "The confidence on this answer is insufficient, please check with the SME.\n"
            "[Confidence Score]: 40"
        )
        == ""
    )


def test_version_merged_sections_filters_noise_and_merges() -> None:
    # The shared version-normalized retrieval (used by BOTH SearchTool and deep-rag):
    # for a >=25 query it retrieves the 2.2510 + 2025.10 variants, keeps only on-version
    # hits per variant (so an AS doc-set's 2.2510 docs win and the 2025.10 variant's
    # off-version noise is dropped even at a higher score), merged + score-sorted.
    from danswer.search.versioning import version_merged_sections

    @dataclass
    class _S:
        document_id: str
        chunk_id: int
        score: float
        semantic_identifier: str
        source_links: dict

    def sec(did: str, ver: str, score: float) -> _S:
        return _S(
            did, 0, score, f"AS - {ver}", {0: f"https://docs.uipath.com/x/{ver}/y"}
        )

    def retrieve_one(variant: str) -> list:
        if "2.2510" in variant:
            return [sec("d1", "2.2510", 0.80), sec("d2", "2.2510", 0.85)]
        if "2025.10" in variant:
            return [sec("noise", "2022.10", 0.95)]  # high score but OFF-version
        return []

    merged = version_merged_sections(retrieve_one, "features in 2025.10")
    # 2022.10 noise dropped despite 0.95; on-version docs kept, sorted by score desc
    assert [s.document_id for s in merged] == ["d2", "d1"]

    # no version token -> single retrieve, nothing filtered
    passthrough = version_merged_sections(
        lambda v: [sec("a", "x", 0.5), sec("b", "y", 0.9)], "how to create a folder"
    )
    assert {s.document_id for s in passthrough} == {"a", "b"}

    # RAW-QUERY FALLBACK: a future/unreleased version (no corpus presence at all) must not
    # return empty — every variant's hits are off-version, so fall back to an unfiltered
    # retrieve of the ORIGINAL query (best-available docs beat nothing). This is what fed
    # deep-rag's per-sub-question collate zero docs on roadmap questions (e.g. 27.10).
    calls: list[str] = []

    def retrieve_future(variant: str) -> list:
        calls.append(variant)
        return [sec("latest", "2.2510", 0.7)]  # only 2.2510 docs exist; 2027.* asked

    fallback = version_merged_sections(retrieve_future, "AI features planned for 27.10")
    assert [s.document_id for s in fallback] == ["latest"]
    # the last call was the raw original query (unfiltered), after the variant calls
    assert calls[-1] == "AI features planned for 27.10"
