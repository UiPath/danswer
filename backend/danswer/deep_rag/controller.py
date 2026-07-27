"""Deep RAG controller — feature-gated, verdict-driven multi-hop.

Flow (only runs when the assistant AND the chat override both enable it):
  1. single-shot answer + self-reported coverage verdict (one LLM call)
  2. coverage == "complete"  -> return single-shot (no escalation, ~1x cost)
  3. coverage == "partial"   -> decompose (cap N, seeded by the verdict's `missing`)
                                -> retrieve per sub-question -> synthesize
                                -> compare-guard: keep the deeper answer only if it is
                                   SUBSTANTIALLY better, else fall back to single-shot

Design notes:
  - Everything except run_deep_rag is a pure function taking an injected `retrieve`
    callable and an `llm`, so the orchestration is unit-testable without Vespa/LLM/DB
    (mirrors the eval harness that validated this logic).
  - Fail-OPEN: any error in the deep path returns the single-shot answer. Deep RAG must
    never make the current experience worse.
  - run_deep_rag retrieves through SearchPipeline with the REAL user + persona (ACL
    enforced). It must never use bypass_acl — that was eval-only.
  - We reuse upstream Onyx's decomposition + comparison prompt learnings (see
    danswer/prompts/deep_rag.py), not its LangGraph.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy.orm import Session

from danswer.db.models import Persona
from danswer.db.models import User
from danswer.llm.factory import get_llms_for_persona
from danswer.llm.interfaces import LLM
from danswer.llm.utils import message_to_string
from danswer.prompts.deep_rag import COMPARISON_PROMPT
from danswer.prompts.deep_rag import DECOMPOSITION_PROMPT
from danswer.prompts.deep_rag import SINGLE_SHOT_VERDICT_PROMPT
from danswer.prompts.deep_rag import SYNTHESIS_PROMPT
from danswer.search.models import SearchRequest
from danswer.search.pipeline import SearchPipeline
from danswer.utils.logger import setup_logger

logger = setup_logger()

# retrieve(query, k) -> top-k sections (any object exposing combined_content/content,
# semantic_identifier, document_id, chunk_id, score). Injected for testability.
RetrieveFn = Callable[[str, int], list[Any]]

DEFAULT_MAX_SUBQUESTIONS = 3
_SINGLE_SHOT_K = 12
_SUBQ_K = 6


@dataclass
class DeepRagResult:
    answer: str
    escalated: bool  # did we run the multi-hop path at all
    source: str  # "single_shot" | "multi_hop"
    coverage: str  # single-shot verdict: "complete" | "partial" | "?"
    sub_questions: list[str] = field(default_factory=list)
    num_sections: int = 0


def _invoke(llm: LLM, prompt: str) -> str:
    return message_to_string(llm.invoke(prompt)).strip()


def _parse_json(text: str) -> dict[str, Any]:
    stripped = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    return json.loads(match.group(0) if match else stripped)


def _format_context(sections: list[Any], start: int = 1) -> str:
    lines = []
    for i, s in enumerate(sections, start):
        ident = getattr(s, "semantic_identifier", None) or getattr(
            s, "document_id", "?"
        )
        content = (
            getattr(s, "combined_content", None) or getattr(s, "content", "") or ""
        )
        lines.append(f"[{i}] ({ident})\n{content[:1100]}")
    return "\n\n".join(lines)


def single_shot(
    query: str, retrieve: RetrieveFn, llm: LLM
) -> tuple[str, str, list[str]]:
    """Answer from one retrieval and self-report coverage. Returns (answer, coverage, missing)."""
    sections = retrieve(query, _SINGLE_SHOT_K)
    prompt = SINGLE_SHOT_VERDICT_PROMPT.format(
        context=_format_context(sections), question=query
    )
    raw = _invoke(llm, prompt)
    try:
        parsed = _parse_json(raw)
        coverage = parsed.get("coverage", "?")
        return (
            str(parsed.get("answer", "")),
            coverage if coverage in ("complete", "partial") else "?",
            [str(m) for m in (parsed.get("missing") or [])],
        )
    except Exception:
        # Unparseable verdict -> treat the raw text as the answer, coverage unknown.
        return raw, "?", []


def decompose(
    query: str, missing: list[str], llm: LLM, max_subquestions: int
) -> list[str]:
    """Break the question into <= max_subquestions sub-questions, seeded by `missing`."""
    hint = ""
    if missing:
        hint = "The initial retrieval did not cover: " + "; ".join(missing) + "\n"
    prompt = DECOMPOSITION_PROMPT.format(
        max_subq=max_subquestions, missing_hint=hint, question=query
    )
    lines = _invoke(llm, prompt).splitlines()
    subqs = [re.sub(r"^[0-9.\-)\s]+", "", ln).strip() for ln in lines]
    subqs = [s for s in subqs if "?" in s][:max_subquestions]
    return subqs or [query]


def multi_hop(
    query: str, sub_questions: list[str], retrieve: RetrieveFn, llm: LLM
) -> tuple[str, int]:
    """Retrieve per sub-question (deduped) and synthesize. Returns (answer, n_sections)."""
    seen: set[tuple[Any, Any]] = set()
    blocks: list[str] = []
    n = 1
    for sq in sub_questions:
        kept = []
        for s in retrieve(sq, _SUBQ_K):
            key = (getattr(s, "document_id", None), getattr(s, "chunk_id", None))
            if key not in seen:
                seen.add(key)
                kept.append(s)
        blocks.append(f"### Sub-question: {sq}\n{_format_context(kept, n)}")
        n += len(kept)
    prompt = SYNTHESIS_PROMPT.format(question=query, context="\n\n".join(blocks))
    return _invoke(llm, prompt), len(seen)


def refined_is_better(query: str, initial: str, refined: str, llm: LLM) -> bool:
    """Compare-guard (upstream INITIAL_REFINED_ANSWER_COMPARISON_PROMPT). Keep the deeper
    answer only if it is SUBSTANTIALLY better. Fail toward the deeper answer on parse issues
    (it was explicitly requested), matching upstream's error handling."""
    prompt = COMPARISON_PROMPT.format(
        question=query, initial_answer=initial, refined_answer=refined
    )
    verdict = _invoke(llm, prompt).strip().lower()
    if "yes" in verdict and "no" not in verdict:
        return True
    if "no" in verdict and "yes" not in verdict:
        return False
    return True  # ambiguous -> keep the refined answer that was asked for


def deep_rag_answer(
    query: str,
    retrieve: RetrieveFn,
    llm: LLM,
    max_subquestions: int = DEFAULT_MAX_SUBQUESTIONS,
) -> DeepRagResult:
    """Verdict-gated multi-hop orchestration. Pure: retrieval + LLM are injected."""
    answer, coverage, missing = single_shot(query, retrieve, llm)

    if coverage != "partial":
        # complete (or unknown) -> single-shot is enough; no escalation.
        return DeepRagResult(
            answer=answer, escalated=False, source="single_shot", coverage=coverage
        )

    # Escalate. Fail-open: any error below returns the single-shot answer.
    try:
        sub_questions = decompose(query, missing, llm, max_subquestions)
        refined, n_sections = multi_hop(query, sub_questions, retrieve, llm)
        if refined_is_better(query, answer, refined, llm):
            return DeepRagResult(
                answer=refined,
                escalated=True,
                source="multi_hop",
                coverage=coverage,
                sub_questions=sub_questions,
                num_sections=n_sections,
            )
        # deeper answer wasn't substantially better -> fall back to single-shot.
        return DeepRagResult(
            answer=answer,
            escalated=True,
            source="single_shot",
            coverage=coverage,
            sub_questions=sub_questions,
            num_sections=n_sections,
        )
    except Exception:
        logger.exception("deep_rag escalation failed; falling back to single-shot")
        return DeepRagResult(
            answer=answer, escalated=True, source="single_shot", coverage=coverage
        )


def _persona_retriever(
    user: User | None, persona: Persona, llm: LLM, fast_llm: LLM, db_session: Session
) -> RetrieveFn:
    """Real retrieval: SearchPipeline with the REAL user + persona (ACL enforced).
    NEVER bypass_acl — that was eval-only."""

    def retrieve(query: str, k: int) -> list[Any]:
        request = SearchRequest(query=query, persona=persona)
        pipeline = SearchPipeline(
            search_request=request,
            user=user,
            llm=llm,
            fast_llm=fast_llm,
            db_session=db_session,
            bypass_acl=False,
        )
        sections = sorted(
            pipeline.retrieved_sections, key=lambda s: (s.score or 0.0), reverse=True
        )
        return sections[:k]

    return retrieve


def run_deep_rag(
    query: str,
    user: User | None,
    persona: Persona,
    db_session: Session,
    max_subquestions: int = DEFAULT_MAX_SUBQUESTIONS,
) -> DeepRagResult:
    """Entry point: wire the real LLM + ACL-scoped retriever into the deep_rag loop."""
    llm, fast_llm = get_llms_for_persona(persona)
    retrieve = _persona_retriever(user, persona, llm, fast_llm, db_session)
    return deep_rag_answer(query, retrieve, llm, max_subquestions=max_subquestions)
