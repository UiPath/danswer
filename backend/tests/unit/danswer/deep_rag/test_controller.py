"""Unit tests for the deep_rag controller — the verdict-gated multi-hop orchestration.

Retrieval and the LLM are stubbed, exactly as in the eval harness that validated the
logic, so these assert the ORCHESTRATION: complete -> no escalation; partial -> escalate;
compare-guard keeps single-shot when the deep answer isn't better; decomposition is
capped; and any deep-path error fails OPEN to the single-shot answer.
"""
from dataclasses import dataclass
from typing import cast

from langchain_core.messages import AIMessage

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


class _ScriptedLLM:
    """Returns a canned response per prompt type (identified by a unique marker)."""

    def __init__(
        self,
        verdict: str,
        subqs: str = "What is A?\nWhat is B?",
        synthesis: str = "REFINED ANSWER",
        comparison: str = "yes",
        raise_on_decompose: bool = False,
    ) -> None:
        self.verdict = verdict
        self.subqs = subqs
        self.synthesis = synthesis
        self.comparison = comparison
        self.raise_on_decompose = raise_on_decompose
        self.calls: list[str] = []

    def invoke(
        self, prompt: object, tools: object = None, tool_choice: object = None
    ) -> AIMessage:
        p = prompt if isinstance(prompt, str) else str(prompt)
        self.calls.append(p)
        if "refined answer" in p:  # compare-guard prompt (compares initial vs refined)
            return AIMessage(content=self.comparison)
        if "no more than" in p:  # decomposition prompt
            if self.raise_on_decompose:
                raise RuntimeError("decompose boom")
            return AIMessage(content=self.subqs)
        if "ORIGINAL question" in p:  # synthesis prompt
            return AIMessage(content=self.synthesis)
        return AIMessage(content=self.verdict)  # single-shot verdict prompt

    def called_decompose(self) -> bool:
        return any("no more than" in c for c in self.calls)


def _complete(answer: str = "single-shot answer") -> str:
    return '{"answer": "%s", "coverage": "complete", "missing": []}' % answer


def _partial(
    answer: str = "partial single-shot", missing: str = '"hardware specs"'
) -> str:
    return '{"answer": "%s", "coverage": "partial", "missing": [%s]}' % (
        answer,
        missing,
    )


def test_complete_verdict_does_not_escalate() -> None:
    llm = _ScriptedLLM(verdict=_complete("A is X"))
    res = deep_rag_answer("q", _retrieve, cast(LLM, llm))
    assert res.source == "single_shot"
    assert res.escalated is False
    assert res.answer == "A is X"
    assert llm.called_decompose() is False  # never entered the deep path


def test_force_escalates_even_when_complete() -> None:
    # the manual per-question override: escalate regardless of a "complete" verdict
    llm = _ScriptedLLM(verdict=_complete("A is X"), comparison="yes")
    res = deep_rag_answer("q", _retrieve, cast(LLM, llm), force=True)
    assert res.escalated is True
    assert res.source == "multi_hop"
    assert llm.called_decompose() is True


def test_partial_escalates_and_keeps_refined_when_better() -> None:
    llm = _ScriptedLLM(verdict=_partial(), comparison="yes")
    res = deep_rag_answer("compare A and B", _retrieve, cast(LLM, llm))
    assert res.escalated is True
    assert res.source == "multi_hop"
    assert res.answer == "REFINED ANSWER"
    assert res.sub_questions == ["What is A?", "What is B?"]
    assert res.num_sections > 0


def test_partial_keeps_refined_even_when_guard_says_not_better() -> None:
    # coverage=partial means single-shot self-reported it was inadequate — that's WHY we
    # escalated — so we keep the refined answer regardless of the compare-guard (the guard
    # only guards the force-on-complete case). Prevents falling back to a punt.
    llm = _ScriptedLLM(verdict=_partial("insufficient, check SME"), comparison="no")
    res = deep_rag_answer("q", _retrieve, cast(LLM, llm))
    assert res.escalated is True
    assert res.source == "multi_hop"
    assert res.answer == "REFINED ANSWER"


def test_forced_on_complete_still_falls_back_when_not_better() -> None:
    # single-shot was COMPLETE but the user forced deep; the compare-guard still applies
    # and keeps single-shot when the deep answer isn't substantially better.
    llm = _ScriptedLLM(verdict=_complete("good enough"), comparison="no")
    res = deep_rag_answer("q", _retrieve, cast(LLM, llm), force=True)
    assert res.escalated is True
    assert res.source == "single_shot"
    assert res.answer == "good enough"


def test_deep_path_error_fails_open_to_single_shot() -> None:
    llm = _ScriptedLLM(verdict=_partial("safe fallback"), raise_on_decompose=True)
    res = deep_rag_answer("q", _retrieve, cast(LLM, llm))
    assert res.source == "single_shot"
    assert res.answer == "safe fallback"


def test_unparseable_verdict_treated_as_answer_no_escalation() -> None:
    llm = _ScriptedLLM(verdict="just prose, not JSON at all")
    res = deep_rag_answer("q", _retrieve, cast(LLM, llm))
    assert res.coverage == "?"
    assert res.escalated is False
    assert res.answer == "just prose, not JSON at all"
    assert llm.called_decompose() is False


def test_decompose_respects_cap() -> None:
    llm = _ScriptedLLM(verdict=_partial(), subqs="a?\nb?\nc?\nd?\ne?")
    subqs = decompose("q", ["x"], cast(LLM, llm), max_subquestions=3)
    assert len(subqs) == 3


def test_decompose_falls_back_to_question_when_empty() -> None:
    llm = _ScriptedLLM(verdict=_partial(), subqs="no question marks here")
    subqs = decompose("original q", [], cast(LLM, llm), max_subquestions=3)
    assert subqs == ["original q"]


def test_history_threaded_into_single_shot_and_decomposition() -> None:
    llm = _ScriptedLLM(verdict=_partial())
    deep_rag_answer(
        "q", _retrieve, cast(LLM, llm), history="User: earlier about pepsico"
    )
    # the history must reach BOTH the single-shot verdict prompt and the decompose prompt
    assert any("earlier about pepsico" in c and "coverage" in c for c in llm.calls)
    assert any("earlier about pepsico" in c and "no more than" in c for c in llm.calls)


def test_empty_history_renders_no_block() -> None:
    llm = _ScriptedLLM(verdict=_complete("A"))
    deep_rag_answer("q", _retrieve, cast(LLM, llm), history="")
    assert not any("Conversation so far" in c for c in llm.calls)


def test_cited_doc_ids_maps_markers_to_sections() -> None:
    from danswer.deep_rag.controller import _cited_doc_ids

    secs = _retrieve("q", 3)  # document_ids d0, d1, d2
    assert _cited_doc_ids("uses [1] and [3].", secs) == {"d0", "d2"}
    assert _cited_doc_ids("no citations here", secs) == set()
    assert _cited_doc_ids("[9] out of range", secs) == set()


def test_apply_authoritative_footer_appends(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import danswer.deep_rag.controller as ctrl
    from danswer.deep_rag.controller import _apply_authoritative_footer
    from danswer.deep_rag.controller import DeepRagResult

    monkeypatch.setattr(ctrl, "AUTHORITATIVE_CITATION_RETENTION_ENABLED", True)
    monkeypatch.setattr(ctrl, "llm_doc_from_inference_section", lambda s: s)
    monkeypatch.setattr(
        ctrl,
        "retained_authoritative_footer",
        lambda ans, docs, cited, llm, question="": "*Authoritative sources*\n• <u|X>",
    )
    r = DeepRagResult(
        answer="base [1]",
        escalated=True,
        source="multi_hop",
        coverage="partial",
        sections=_retrieve("q", 2),
    )
    _apply_authoritative_footer(r, "q", cast(LLM, object()))
    assert r.answer.startswith("base [1]") and "Authoritative sources" in r.answer


def test_apply_authoritative_footer_noop_when_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import danswer.deep_rag.controller as ctrl
    from danswer.deep_rag.controller import _apply_authoritative_footer
    from danswer.deep_rag.controller import DeepRagResult

    monkeypatch.setattr(ctrl, "AUTHORITATIVE_CITATION_RETENTION_ENABLED", False)
    r = DeepRagResult(
        answer="base",
        escalated=False,
        source="single_shot",
        coverage="complete",
        sections=_retrieve("q", 2),
    )
    _apply_authoritative_footer(r, "q", cast(LLM, object()))
    assert r.answer == "base"


def test_persona_prompt_injected_into_deep_rag_prompts() -> None:
    # the assistant's system/task prompt must reach BOTH the single-shot and the
    # synthesis prompt, so deep answers honor its format/SME facts/routing rules
    llm = _ScriptedLLM(
        verdict=_partial("initial"),
        subqs="what is a?\nwhat is b?",
        synthesis="deep answer [1]",
        comparison="yes",
    )
    persona = "SME-RULE: Scott Schoenberger is the SME for AI connectors."
    deep_rag_answer("q", _retrieve, cast(LLM, llm), force=True, persona_prompt=persona)
    single_shot_calls = [c for c in llm.calls if "JSON:" in c]
    synthesis_calls = [c for c in llm.calls if "ORIGINAL question" in c]
    assert single_shot_calls and all(persona in c for c in single_shot_calls)
    assert synthesis_calls and all(persona in c for c in synthesis_calls)
    # generic identity fallback is NOT used when a persona prompt is present
    assert not any(
        "You are a UiPath documentation assistant." in c for c in single_shot_calls
    )


def test_default_identity_when_no_persona_prompt() -> None:
    llm = _ScriptedLLM(verdict=_complete("A"))
    deep_rag_answer("q", _retrieve, cast(LLM, llm))  # persona_prompt="" (default)
    ss = [c for c in llm.calls if "JSON:" in c]
    assert ss and all("You are a UiPath documentation assistant." in c for c in ss)


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


def test_version_query_variants() -> None:
    from danswer.deep_rag.controller import _version_query_variants

    # <=24.MM -> YYYY.MM only, replaced in place
    assert _version_query_variants("features in 24.10") == ["features in 2024.10"]
    # >=25.MM -> suite (2.NNMM) + desktop (YYYY.MM)
    assert _version_query_variants("features in 25.10") == [
        "features in 2.2510",
        "features in 2025.10",
    ]
    # multi-version: each token rewritten per variant; 24.10 reuses its only form
    assert _version_query_variants("diff 25.10 vs 24.10") == [
        "diff 2.2510 vs 2024.10",
        "diff 2025.10 vs 2024.10",
    ]
    # 4-digit spelling must expand too: "2025.10" -> {2.2510, 2025.10} (decomposition
    # sometimes emits the year form, which is still WRONG for Automation Suite)
    assert _version_query_variants("features in 2025.10") == [
        "features in 2.2510",
        "features in 2025.10",
    ]
    # 4-digit <=24 is already the corpus form -> single, unchanged
    assert _version_query_variants("features in 2024.10") == ["features in 2024.10"]
    # mixed 4-digit + 2-digit in one query, rewritten positionally in one pass
    assert _version_query_variants("diff 2025.10 vs 24.10") == [
        "diff 2.2510 vs 2024.10",
        "diff 2025.10 vs 2024.10",
    ]
    # no version token -> single unchanged query
    assert _version_query_variants("how to create a folder") == [
        "how to create a folder"
    ]
    # minor zero-padded in the 2.NNMM form
    assert _version_query_variants("suite 25.4")[0] == "suite 2.2504"
