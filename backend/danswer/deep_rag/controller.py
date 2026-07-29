"""Deep RAG controller — feature-gated, verdict-driven multi-hop.

Callers:
  - Web chat: `force=True` (per-question toggle) — always escalate.
  - Slack bot: `force=False`, gated on the assistant's deep_rag_enabled — adaptive.

Flow:
  1. single-shot answer + self-reported coverage verdict (one LLM call)
  2. coverage == "complete" and not force -> return single-shot (no escalation, ~1x)
  3. otherwise (partial, or forced) -> decompose (cap N, seeded by `missing`)
                                -> retrieve per sub-question -> synthesize
                                -> compare-guard: keep the deeper answer only if it is
                                   SUBSTANTIALLY better, else fall back to single-shot

Design notes:
  - Everything except run_deep_rag is a pure function taking an injected `retrieve`
    callable and an `llm`, so the orchestration is unit-testable without Vespa/LLM/DB
    (mirrors the eval harness that validated this logic).
  - Fail-OPEN: any error in the deep path returns the single-shot answer. Deep RAG must
    never make the current experience worse.
  - Retrieval enforces the REAL user's ACL by default; `bypass_acl=True` only for
    surfaces that manage access differently (the Slack bot, user=None). Never bypass
    in the web request path.
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

from danswer.chat.chat_utils import llm_doc_from_inference_section
from danswer.configs.chat_configs import AUTHORITATIVE_CITATION_RETENTION_ENABLED
from danswer.db.models import Persona
from danswer.db.models import User
from danswer.llm.answering.authoritative_retention import retained_authoritative_footer
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
    # Ordered sections behind the final answer's [n] citation markers, so the caller
    # can surface documents + map citations. Index i corresponds to marker [i+1].
    sections: list[Any] = field(default_factory=list)


def _invoke(llm: LLM, prompt: str) -> str:
    return message_to_string(llm.invoke(prompt)).strip()


def _parse_json(text: str) -> dict[str, Any]:
    stripped = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    return json.loads(match.group(0) if match else stripped)


def _history_block(history: str) -> str:
    """Render the conversation history for the prompts, or "" when there is none.
    Threaded into single-shot and decomposition so follow-up questions resolve in
    context (mirrors upstream's history-aware decomposition)."""
    if not history or not history.strip():
        return ""
    return (
        "Conversation so far (for context; the current question may be a follow-up "
        "that depends on it):\n" + history.strip() + "\n\n"
    )


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
    query: str, retrieve: RetrieveFn, llm: LLM, history: str = ""
) -> tuple[str, str, list[str], list[Any]]:
    """Answer from one retrieval and self-report coverage.
    Returns (answer, coverage, missing, sections). `sections` is the ordered
    retrieval behind the answer's [n] markers."""
    sections = retrieve(query, _SINGLE_SHOT_K)
    prompt = SINGLE_SHOT_VERDICT_PROMPT.format(
        context=_format_context(sections),
        question=query,
        history=_history_block(history),
    )
    raw = _invoke(llm, prompt)
    try:
        parsed = _parse_json(raw)
        coverage = parsed.get("coverage", "?")
        return (
            str(parsed.get("answer", "")),
            coverage if coverage in ("complete", "partial") else "?",
            [str(m) for m in (parsed.get("missing") or [])],
            sections,
        )
    except Exception:
        # Unparseable verdict -> treat the raw text as the answer, coverage unknown.
        return raw, "?", [], sections


def decompose(
    query: str, missing: list[str], llm: LLM, max_subquestions: int, history: str = ""
) -> list[str]:
    """Break the question into <= max_subquestions sub-questions, seeded by `missing`."""
    hint = ""
    if missing:
        hint = "The initial retrieval did not cover: " + "; ".join(missing) + "\n"
    prompt = DECOMPOSITION_PROMPT.format(
        max_subq=max_subquestions,
        missing_hint=hint,
        history=_history_block(history),
        question=query,
    )
    lines = _invoke(llm, prompt).splitlines()
    subqs = [re.sub(r"^[0-9.\-)\s]+", "", ln).strip() for ln in lines]
    subqs = [s for s in subqs if "?" in s][:max_subquestions]
    return subqs or [query]


def multi_hop(
    query: str, sub_questions: list[str], retrieve: RetrieveFn, llm: LLM
) -> tuple[str, list[Any]]:
    """Retrieve per sub-question (deduped) and synthesize.
    Returns (answer, ordered_sections) where ordered_sections[i] is the section
    numbered [i+1] in the synthesis context (so the caller can map citations)."""
    seen: set[tuple[Any, Any]] = set()
    ordered: list[Any] = []
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
        ordered.extend(kept)
        n += len(kept)
    prompt = SYNTHESIS_PROMPT.format(question=query, context="\n\n".join(blocks))
    return _invoke(llm, prompt), ordered


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
    history: str = "",
    force: bool = False,
) -> DeepRagResult:
    """Multi-hop orchestration. Pure: retrieval + LLM are injected.

    `force=False` (adaptive): single-shot first, escalate only if the coverage
    verdict is `partial`. `force=True`: always escalate regardless of the verdict —
    the per-question manual override. Even when forced the compare-guard still keeps
    single-shot if the deep answer isn't substantially better, so forcing can't
    produce a worse answer, only extra work.
    """
    answer, coverage, missing, ss_sections = single_shot(
        query, retrieve, llm, history=history
    )

    if coverage != "partial" and not force:
        # complete (or unknown) + not forced -> single-shot is enough; no escalation.
        return DeepRagResult(
            answer=answer,
            escalated=False,
            source="single_shot",
            coverage=coverage,
            sections=ss_sections,
            num_sections=len(ss_sections),
        )

    # Escalate. Fail-open: any error below returns the single-shot answer.
    try:
        sub_questions = decompose(
            query, missing, llm, max_subquestions, history=history
        )
        refined, mh_sections = multi_hop(query, sub_questions, retrieve, llm)
        if refined_is_better(query, answer, refined, llm):
            return DeepRagResult(
                answer=refined,
                escalated=True,
                source="multi_hop",
                coverage=coverage,
                sub_questions=sub_questions,
                num_sections=len(mh_sections),
                sections=mh_sections,
            )
        # deeper answer wasn't substantially better -> fall back to single-shot
        # (so the [n] markers map to the single-shot sections, not the multi-hop ones).
        return DeepRagResult(
            answer=answer,
            escalated=True,
            source="single_shot",
            coverage=coverage,
            sub_questions=sub_questions,
            num_sections=len(ss_sections),
            sections=ss_sections,
        )
    except Exception:
        logger.exception("deep_rag escalation failed; falling back to single-shot")
        return DeepRagResult(
            answer=answer,
            escalated=True,
            source="single_shot",
            coverage=coverage,
            sections=ss_sections,
            num_sections=len(ss_sections),
        )


def _persona_retriever(
    user: User | None,
    persona: Persona,
    llm: LLM,
    fast_llm: LLM,
    db_session: Session,
    bypass_acl: bool = False,
) -> RetrieveFn:
    """Retrieval via SearchPipeline scoped to the persona. Enforces the REAL user's
    ACL by default (web chat). `bypass_acl=True` only for surfaces that manage access
    differently — e.g. the Slack bot, which retrieves with user=None like its normal
    answer flow. Never bypass in the web request path."""

    def retrieve(query: str, k: int) -> list[Any]:
        request = SearchRequest(query=query, persona=persona)
        pipeline = SearchPipeline(
            search_request=request,
            user=user,
            llm=llm,
            fast_llm=fast_llm,
            db_session=db_session,
            bypass_acl=bypass_acl,
        )
        sections = sorted(
            pipeline.retrieved_sections, key=lambda s: (s.score or 0.0), reverse=True
        )
        return sections[:k]

    return retrieve


def _cited_doc_ids(answer: str, sections: list[Any]) -> set[str]:
    """document_ids behind the [n] markers the answer actually cited."""
    ids: set[str] = set()
    for m in re.findall(r"\[(\d+)\]", answer):
        i = int(m) - 1
        if 0 <= i < len(sections):
            did = getattr(sections[i], "document_id", None)
            if did:
                ids.add(str(did))
    return ids


def _apply_authoritative_footer(result: DeepRagResult, query: str, llm: LLM) -> None:
    """Parity with the standard answer pipeline (answer.py): append an "Authoritative
    sources" footer for relevant PROTECTED_SOURCES docs that are in the deep-rag context
    but were NOT cited. Deep RAG runs its own synthesis and never hits answer.py where
    this normally runs, so without this the footer is missing on deep answers. Gated by
    the same flag; fail-open (a footer failure never breaks the answer)."""
    if not AUTHORITATIVE_CITATION_RETENTION_ENABLED or not result.sections:
        return
    try:
        docs = [llm_doc_from_inference_section(s) for s in result.sections]
        cited = _cited_doc_ids(result.answer, result.sections)
        footer = retained_authoritative_footer(
            result.answer, docs, cited, llm, question=query
        )
        if footer:
            result.answer = result.answer + "\n\n" + footer
    except Exception:
        logger.exception("deep_rag authoritative retention failed; skipping footer")


def run_deep_rag(
    query: str,
    user: User | None,
    persona: Persona,
    db_session: Session,
    max_subquestions: int = DEFAULT_MAX_SUBQUESTIONS,
    history: str = "",
    force: bool = False,
    bypass_acl: bool = False,
) -> DeepRagResult:
    """Entry point: wire the LLM + persona-scoped retriever into the deep_rag loop.

    `history` is the serialized prior conversation (empty for the first turn), so
    follow-up questions decompose/answer in context. `force=True` always escalates
    (the web manual override); `force=False` is verdict-gated (adaptive — used by
    Slack). `bypass_acl=True` for surfaces like the Slack bot (see _persona_retriever).
    """
    llm, fast_llm = get_llms_for_persona(persona)
    retrieve = _persona_retriever(
        user, persona, llm, fast_llm, db_session, bypass_acl=bypass_acl
    )
    result = deep_rag_answer(
        query,
        retrieve,
        llm,
        max_subquestions=max_subquestions,
        history=history,
        force=force,
    )
    _apply_authoritative_footer(result, query, llm)
    return result
