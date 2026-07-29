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


def test_partial_but_refined_not_better_falls_back_to_single_shot() -> None:
    llm = _ScriptedLLM(verdict=_partial("keep me"), comparison="no")
    res = deep_rag_answer("q", _retrieve, cast(LLM, llm))
    assert res.escalated is True  # we did try
    assert res.source == "single_shot"  # ...but kept the single-shot answer
    assert res.answer == "keep me"


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
