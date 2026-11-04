"""Unit tests for the answer-trace collector — the contextvar-scoped provenance
accumulator. A stub Session captures the persisted row so no DB is needed.
"""
from typing import Any

import pytest

from danswer.answer_trace import tracer
from danswer.answer_trace.tracer import current_trace
from danswer.answer_trace.tracer import event
from danswer.answer_trace.tracer import finalize_and_persist
from danswer.answer_trace.tracer import record_deep_rag
from danswer.answer_trace.tracer import set_fields
from danswer.answer_trace.tracer import start_trace


class _StubSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


@pytest.fixture(autouse=True)
def _reset_trace() -> None:
    # isolate the module-level contextvar between tests
    tracer._CURRENT.set(None)


def test_calls_are_noops_without_a_started_trace() -> None:
    # this is also the "tracing disabled" path (start_trace never sets the var)
    event("route", {"x": 1})
    record_deep_rag(
        escalated=True, coverage="partial", sub_questions=["a?"], source="x"
    )
    set_fields(cited_source_count=3)
    assert current_trace() is None
    assert finalize_and_persist(_StubSession(), chat_message_id=1) is None


def test_start_then_event_accumulates() -> None:
    start_trace(surface="chat", query="q", persona_id=315, chat_session_id=5)
    event("retrieve", {"candidates": 12}, duration_ms=310)
    t = current_trace()
    assert t is not None
    assert t.surface == "chat" and t.persona_id == 315
    assert t.events == [
        {"stage": "retrieve", "data": {"candidates": 12}, "duration_ms": 310}
    ]


def test_record_deep_rag_escalated() -> None:
    start_trace(surface="chat", query="compare A and B")
    record_deep_rag(
        escalated=True,
        coverage="partial",
        sub_questions=["A?", "B?"],
        source="multi_hop",
    )
    t = current_trace()
    assert t is not None
    assert t.deep_rag_enabled is True
    assert t.deep_rag_escalated is True
    assert t.sub_question_count == 2
    stages = [e["stage"] for e in t.events]
    assert stages == [
        "deep_rag.single_shot",
        "deep_rag.decompose",
        "deep_rag.compare_guard",
    ]


def test_record_deep_rag_not_escalated_only_single_shot() -> None:
    start_trace(surface="chat", query="q")
    record_deep_rag(
        escalated=False, coverage="complete", sub_questions=[], source="single_shot"
    )
    t = current_trace()
    assert t is not None
    assert t.deep_rag_escalated is False
    assert t.sub_question_count == 0
    assert [e["stage"] for e in t.events] == ["deep_rag.single_shot"]


def test_set_fields_updates_promoted_columns() -> None:
    start_trace(surface="search", query="q")
    set_fields(
        cited_source_count=3, reranked=True, route_stage="knn", route_confidence=0.62
    )
    t = current_trace()
    assert t is not None
    assert t.cited_source_count == 3 and t.reranked is True
    assert t.route_stage == "knn" and t.route_confidence == 0.62


def test_finalize_persists_row_and_clears() -> None:
    start_trace(surface="chat", query="q", persona_id=1, chat_session_id=9)
    set_fields(cited_source_count=4)
    stub = _StubSession()
    trace_uuid = finalize_and_persist(stub, chat_message_id=7)
    assert trace_uuid is not None
    assert len(stub.added) == 1
    row = stub.added[0]
    assert row.chat_message_id == 7
    assert row.cited_source_count == 4
    assert row.surface == "chat"
    assert isinstance(row.total_duration_ms, int)
    # the contextvar is cleared after finalize
    assert current_trace() is None
