"""Request-scoped answer-provenance collector.

A single answer's pipeline decisions are accumulated in a contextvar-scoped trace
(no signature threading), then written as one `answer_trace` row. When tracing is
disabled every function here is a no-op, so the hot answer path is unchanged.

Design rules:
  - Record DETERMINISTIC decisions actually taken, at the point they're taken.
    No reconstructed or LLM-narrated reasoning.
  - Events reference doc/chunk IDENTIFIERS, never chunk content.
  - contextvars do NOT propagate into worker threads (SearchPipeline's parallel
    fetch). Capture branch-level decisions on the main request thread; per-thread
    internals are out of scope for v1.

See ANSWER_TRACE.md and db/models.py::AnswerTrace.
"""
import os
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from danswer.db.models import AnswerTrace
from danswer.utils.logger import setup_logger

logger = setup_logger()

ANSWER_TRACE_ENABLED = os.environ.get("ANSWER_TRACE_ENABLED", "true").lower() == "true"
ANSWER_TRACE_RETENTION_DAYS = int(os.environ.get("ANSWER_TRACE_RETENTION_DAYS", "14"))


@dataclass
class TraceData:
    trace_uuid: uuid.UUID
    surface: str
    query: str
    persona_id: int | None
    chat_session_id: int | None
    user_id: uuid.UUID | None
    _t0: float
    rephrased_query: str | None = None
    route_stage: str | None = None
    route_confidence: float | None = None
    retrieved_count: int | None = None
    reranked: bool = False
    cited_source_count: int | None = None
    deep_rag_enabled: bool = False
    deep_rag_escalated: bool = False
    sub_question_count: int | None = None
    model: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


_CURRENT: ContextVar[TraceData | None] = ContextVar("answer_trace", default=None)


def start_trace(
    surface: str,
    query: str,
    persona_id: int | None = None,
    chat_session_id: int | None = None,
    user_id: uuid.UUID | None = None,
) -> TraceData | None:
    """Begin a trace for this request (no-op + returns None when disabled)."""
    if not ANSWER_TRACE_ENABLED:
        return None
    data = TraceData(
        trace_uuid=uuid.uuid4(),
        surface=surface,
        query=query,
        persona_id=persona_id,
        chat_session_id=chat_session_id,
        user_id=user_id,
        _t0=time.monotonic(),
    )
    _CURRENT.set(data)
    return data


def current_trace() -> TraceData | None:
    return _CURRENT.get()


def event(stage: str, data: dict[str, Any], duration_ms: int | None = None) -> None:
    """Append one ordered stage event to the current trace (no-op if none)."""
    t = _CURRENT.get()
    if t is None:
        return
    entry: dict[str, Any] = {"stage": stage, "data": data}
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms
    t.events.append(entry)


def record_deep_rag(
    escalated: bool,
    coverage: str,
    sub_questions: list[str],
    source: str,
    escalation_reason: str = "",
) -> None:
    """Fold a DeepRagResult's branch decisions into the current trace.

    `escalation_reason` is why we escalated — "verdict_partial" | "no_citations" |
    "low_confidence" | "forced" | "" — so the trace distinguishes the escalation signals
    (the recorded `coverage` is the effective, possibly-downgraded value)."""
    t = _CURRENT.get()
    if t is None:
        return
    t.deep_rag_enabled = True
    t.deep_rag_escalated = escalated
    t.sub_question_count = len(sub_questions)
    event("deep_rag.single_shot", {"coverage": coverage})
    if escalated:
        event("deep_rag.decompose", {"sub_questions": sub_questions})
        event(
            "deep_rag.compare_guard",
            {"kept": source, "escalation_reason": escalation_reason},
        )


def set_fields(**kwargs: Any) -> None:
    """Set promoted scalar fields on the current trace (no-op if none)."""
    t = _CURRENT.get()
    if t is None:
        return
    for key, value in kwargs.items():
        if hasattr(t, key):
            setattr(t, key, value)


def finalize_and_persist(
    db_session: Session, chat_message_id: int | None = None
) -> uuid.UUID | None:
    """Build and add the answer_trace row (caller commits). No-op if disabled/absent.

    Never lets a trace failure break the answer: on any error we log and drop the
    trace rather than propagate.
    """
    t = _CURRENT.get()
    if t is None:
        return None
    try:
        row = AnswerTrace(
            trace_uuid=t.trace_uuid,
            chat_message_id=chat_message_id,
            chat_session_id=t.chat_session_id,
            user_id=t.user_id,
            surface=t.surface,
            persona_id=t.persona_id,
            query=t.query,
            rephrased_query=t.rephrased_query,
            route_stage=t.route_stage,
            route_confidence=t.route_confidence,
            retrieved_count=t.retrieved_count,
            reranked=t.reranked,
            cited_source_count=t.cited_source_count,
            deep_rag_enabled=t.deep_rag_enabled,
            deep_rag_escalated=t.deep_rag_escalated,
            sub_question_count=t.sub_question_count,
            model=t.model,
            total_duration_ms=int((time.monotonic() - t._t0) * 1000),
            events=t.events,
        )
        db_session.add(row)
        return t.trace_uuid
    except Exception:
        logger.exception("failed to persist answer_trace; dropping it")
        return None
    finally:
        _CURRENT.set(None)


def prune_old_traces(
    db_session: Session, days: int = ANSWER_TRACE_RETENTION_DAYS
) -> int:
    """Delete answer_trace rows older than `days`. Returns rows removed.
    Intended to run from the periodic cleanup job."""
    result = db_session.execute(
        text(
            "DELETE FROM answer_trace "
            "WHERE created_at < now() - make_interval(days => :days)"
        ),
        {"days": days},
    )
    db_session.commit()
    # rowcount is present at runtime for DML but not on the Result type stub
    return getattr(result, "rowcount", 0) or 0
