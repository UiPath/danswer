"""Deep RAG controller — feature-gated, verdict-driven multi-hop.

Callers:
  - Web chat: `force=True` (per-question toggle) — always escalate.
  - Slack bot: `force=False`, gated on the assistant's deep_rag_enabled — adaptive.

Flow:
  1. single-shot answer (ONE normal-flow answer call, prompt untouched) + coverage
     verdict from a separate fast-LLM judge over the finished question+answer
  2. coverage == "complete" and not force -> return single-shot (no escalation, ~1x)
  3. otherwise (partial, or forced) -> decompose (cap N, seeded by `missing`)
                                -> retrieve per sub-question -> collate/cap
                                -> synthesize over the collated docs
                                -> compare-guard: keep the deeper answer only if it is
                                   SUBSTANTIALLY better, else fall back to single-shot

Design notes:
  - ALL answer generation (single-shot AND multi-hop synthesis) runs on the well-tested
    normal answer flow (`get_search_answer` -> `Answer` + `SearchTool`), so deep RAG gets
    the persona prompt, inline [n] citations, quotes, reflexion, doc-pruning and the
    authoritative-source footer for free (parity — no re-implementation to drift). Multi-hop
    feeds its curated per-sub-question chunks through `SearchTool(selected_docs=...)`.
  - `deep_rag_answer` is pure: the answer generator (`generate`), the per-sub-question
    retriever (`retrieve`) and the decompose/compare `llm` are injected, so the
    orchestration is unit-testable without Vespa/LLM/DB.
  - Fail-OPEN: a missing/unparseable verdict is treated as `complete` (no escalation); any
    error in the deep path returns the single-shot answer. Deep RAG must never make the
    current experience worse.
  - Retrieval enforces the REAL user's ACL by default; `bypass_acl=True` only for
    surfaces that manage access differently (the Slack bot, user=None). Never bypass
    in the web request path.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy.orm import Session

from danswer.chat.chat_utils import llm_doc_from_inference_section
from danswer.chat.models import CitationInfo
from danswer.chat.models import LlmDoc
from danswer.configs.chat_configs import PROTECTED_SOURCES
from danswer.configs.chat_configs import SOURCE_DIVERSITY_RESERVED_SLOTS
from danswer.configs.constants import DocumentSource
from danswer.configs.constants import MessageType
from danswer.db.models import Persona
from danswer.db.models import User
from danswer.llm.factory import get_llms_for_persona
from danswer.llm.interfaces import LLM
from danswer.llm.utils import message_to_string
from danswer.one_shot_answer.answer_question import get_search_answer
from danswer.one_shot_answer.models import DirectQARequest
from danswer.one_shot_answer.models import ThreadMessage
from danswer.prompts.deep_rag import COMPARISON_PROMPT
from danswer.prompts.deep_rag import DECOMPOSITION_PROMPT
from danswer.prompts.deep_rag import VERDICT_JUDGE_PROMPT
from danswer.search.models import SearchRequest
from danswer.search.pipeline import SearchPipeline
from danswer.search.utils import chunks_or_sections_to_search_docs
from danswer.search.versioning import version_merged_sections
from danswer.utils.logger import setup_logger

logger = setup_logger()

# retrieve(query, k) -> top-k sections (any object exposing combined_content/content,
# semantic_identifier, document_id, chunk_id, score). Injected for testability.
RetrieveFn = Callable[[str, int], list[Any]]

# CEILING on sub-questions per escalation, not a target — the decomposition prompt tells
# the model to use as few as the question actually needs (a simple question gets one).
# Raise via env when questions in a deployment routinely have more distinct parts.
DEFAULT_MAX_SUBQUESTIONS = int(os.environ.get("DEEP_RAG_MAX_SUBQUESTIONS") or 3)
_SUBQ_K = 6
# How many distinct PROTECTED_SOURCES documents to retain from EACH sub-question's
# retrieval (highest-scored). Applied per sub-question (not globally) so each
# single-version sub-question keeps its own curated docs — a global by-score cap would
# collapse a version comparison onto whichever version scores higher. Configurable;
# default 3 gives richer, more-specific comparisons (measured) at a modest token cost.
DEEP_RAG_SUBQ_PROTECTED_DOCS = int(os.environ.get("DEEP_RAG_SUBQ_PROTECTED_DOCS") or 3)

# A single-shot answer self-reporting a confidence score AT OR BELOW this (0-100) is
# treated as 'partial' so it escalates. Default 80: self-scores are noisy (the same
# question rolls 70-95 across runs), and a boundary at the persona's "moderate" band
# (85) made borderline-but-fine answers escalate probabilistically; <=80 reserves the
# deeper pass for answers the model itself marks clearly shaky.
# Personas that don't print a "Confidence Score: NN" line leave this signal inert.
DEEP_RAG_MIN_CONFIDENCE = int(os.environ.get("DEEP_RAG_MIN_CONFIDENCE") or 80)

# Delimiter of the coverage-verdict block in the judge call's output
# (see VERDICT_JUDGE_PROMPT); parsed by _split_verdict.
_VERDICT_MARKER = "---VERDICT---"


@dataclass
class DeepRagResult:
    answer: str
    escalated: bool  # did we run the multi-hop path at all
    source: str  # "single_shot" | "multi_hop"
    coverage: str  # single-shot verdict: "complete" | "partial" | "?"
    sub_questions: list[str] = field(default_factory=list)
    # Normal-flow outputs behind the final answer, so the caller surfaces sources +
    # resolves citations WITHOUT re-parsing the answer text: `citations` maps each [n]
    # marker to a document_id (already renumbered by the answer flow), `docs` are the
    # server search docs to persist.
    citations: list[CitationInfo] = field(default_factory=list)
    docs: list[Any] = field(default_factory=list)  # server search docs to persist
    # Why we escalated, for the trace: "" (didn't escalate) | "verdict_partial" |
    # "no_citations" | "low_confidence" | "forced".
    escalation_reason: str = ""


@dataclass
class _Gen:
    """One normal-flow answer, plus the coverage verdict when it was requested."""

    answer: str
    citations: list[CitationInfo]
    docs: list[Any]  # server search docs behind the answer
    coverage: str  # "complete" | "partial" (fail-open "complete" when no verdict)
    missing: list[str]


# generate(question, *, selected_docs, want_verdict) -> _Gen. Injected into
# deep_rag_answer so the orchestration is testable without the real answer flow.
GenerateFn = Callable[..., _Gen]


def _invoke(llm: LLM, prompt: str) -> str:
    return message_to_string(llm.invoke(prompt)).strip()


def _parse_json(text: str) -> dict[str, Any]:
    stripped = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    return json.loads(match.group(0) if match else stripped)


def _history_block(history: str) -> str:
    """Render the conversation history for the prompts, or "" when there is none.
    Threaded into decomposition so follow-up questions resolve in context (mirrors
    upstream's history-aware decomposition)."""
    if not history or not history.strip():
        return ""
    return (
        "Conversation so far (for context; the current question may be a follow-up "
        "that depends on it):\n" + history.strip() + "\n\n"
    )


def _split_verdict(answer: str) -> tuple[str, str, list[str]]:
    """Split the trailing ---VERDICT--- block off a single-shot answer.
    Returns (clean_answer, coverage, missing). Fail-OPEN: a missing or unparseable
    verdict yields coverage="complete" (no escalation), never raises."""
    idx = answer.rfind(_VERDICT_MARKER)
    if idx == -1:
        return answer, "complete", []
    clean = answer[:idx].rstrip()
    try:
        parsed = _parse_json(answer[idx + len(_VERDICT_MARKER) :])
        coverage = parsed.get("coverage")
        coverage = coverage if coverage in ("complete", "partial") else "complete"
        missing = [str(m) for m in (parsed.get("missing") or [])]
        return clean, coverage, missing
    except Exception:
        return clean, "complete", []


_CONFIDENCE_RE = re.compile(r"confidence(?:\s*score)?\D{0,12}(\d{1,3})", re.IGNORECASE)


def _below_confidence_threshold(answer: str) -> bool:
    """True when the answer self-reports a confidence score at or below DEEP_RAG_MIN_CONFIDENCE.
    Matches a persona's "Confidence Score: NN" line; absent / unparseable / out-of-range
    -> False (personas that don't emit a score are simply unaffected)."""
    m = _CONFIDENCE_RE.search(answer or "")
    if not m:
        return False
    score = int(m.group(1))
    return 0 <= score <= 100 and score <= DEEP_RAG_MIN_CONFIDENCE


def _to_gen(resp: Any, coverage: str, missing: list[str]) -> _Gen:
    docs = resp.docs.top_documents if resp.docs else []
    return _Gen(
        answer=resp.answer or "",
        citations=resp.citations or [],
        docs=list(docs),
        coverage=coverage,
        missing=missing,
    )


def decompose(
    query: str,
    missing: list[str],
    llm: LLM,
    max_subquestions: int,
    history: str = "",
    subject: str = "",
) -> list[str]:
    """Break the question into <= max_subquestions sub-questions, seeded by `missing`.
    `subject` is the answering assistant's scope (persona name/description) — questions
    often name the product only implicitly ("our document", "the installer"), so the
    context-retention rule needs the assistant's own subject to anchor sub-questions to;
    without it a generic question decomposes into platform-generic sub-questions."""
    hint = ""
    if missing:
        hint = "The initial retrieval did not cover: " + "; ".join(missing) + "\n"
    subject_block = (
        (
            "The question was asked to an assistant whose subject is: "
            + subject.strip()
            + ". Interpret the question in that subject's context and keep every "
            "sub-question anchored to it.\n"
        )
        if subject and subject.strip()
        else ""
    )
    prompt = DECOMPOSITION_PROMPT.format(
        max_subq=max_subquestions,
        subject_block=subject_block,
        missing_hint=hint,
        history=_history_block(history),
        question=query,
    )
    lines = _invoke(llm, prompt).splitlines()
    subqs = [re.sub(r"^[0-9.\-)\s]+", "", ln).strip() for ln in lines]
    subqs = [s for s in subqs if "?" in s][:max_subquestions]
    return subqs or [query]


def _collate(sub_questions: list[str], retrieve: RetrieveFn) -> list[Any]:
    """Retrieve per sub-question (deduped across sub-questions) and cap protected docs
    per sub-question. Returns the ordered curated sections to synthesize over — this is
    the only novel retrieval step; synthesis itself runs on the normal answer flow."""
    seen: set[tuple[Any, Any]] = set()
    ordered: list[Any] = []
    for sq in sub_questions:
        kept = []
        for s in retrieve(sq, _SUBQ_K):
            key = (getattr(s, "document_id", None), getattr(s, "chunk_id", None))
            if key not in seen:
                seen.add(key)
                kept.append(s)
        # Keep only the top DEEP_RAG_SUBQ_PROTECTED_DOCS curated documents from THIS
        # sub-question (all non-protected kept). Per sub-question, so each single-version
        # sub-question retains its own curated docs — a global cap would drop the
        # lower-scored version entirely on a comparison.
        ordered.extend(_cap_protected_docs(kept, DEEP_RAG_SUBQ_PROTECTED_DOCS))
    return ordered


# The persona appends an "[SME]: …" referral and a "[Confidence Score]: NN" line (and,
# on a punt, "The confidence on this answer is insufficient…") to every answer. That's
# presentation boilerplate, not answer substance — strip it before the compare-guard so
# the judge weighs only the actual content, not the self-reported confidence numbers or
# the SME deferral. (The displayed answer keeps its footer; only the judge input is stripped.)
_JUDGE_NOISE_RE = re.compile(
    r"^\s*(?:\**\[?\s*(?:SME|Confidence(?:\s+Score)?)\s*\]?\**\s*:.*"
    r"|The confidence on this answer is insufficient[^\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_judge_noise(text: str) -> str:
    """Remove the SME-referral / confidence-score footer so the compare-guard judges only
    the substantive answer content."""
    return _JUDGE_NOISE_RE.sub("", text or "").strip()


def refined_is_better(query: str, initial: str, refined: str, llm: LLM) -> bool:
    """Compare-guard (upstream INITIAL_REFINED_ANSWER_COMPARISON_PROMPT). Keep the deeper
    answer only if it is SUBSTANTIALLY better. Compares the answers with their SME /
    confidence-score footer stripped (that boilerplate is not answer substance). Fails
    toward the deeper answer on parse issues (it was explicitly requested)."""
    prompt = COMPARISON_PROMPT.format(
        question=query,
        initial_answer=_strip_judge_noise(initial),
        refined_answer=_strip_judge_noise(refined),
    )
    verdict = _invoke(llm, prompt).strip().lower()
    if "yes" in verdict and "no" not in verdict:
        return True
    if "no" in verdict and "yes" not in verdict:
        return False
    return True  # ambiguous -> keep the refined answer that was asked for


def _result(
    gen: _Gen,
    *,
    escalated: bool,
    source: str,
    coverage: str,
    sub_questions: list[str] | None = None,
    escalation_reason: str = "",
) -> DeepRagResult:
    return DeepRagResult(
        answer=gen.answer,
        escalated=escalated,
        source=source,
        coverage=coverage,
        sub_questions=sub_questions or [],
        citations=gen.citations,
        docs=gen.docs,
        escalation_reason=escalation_reason,
    )


def deep_rag_answer(
    query: str,
    generate: GenerateFn,
    retrieve: RetrieveFn,
    llm: LLM,
    max_subquestions: int = DEFAULT_MAX_SUBQUESTIONS,
    history: str = "",
    force: bool = False,
    subject: str = "",
) -> DeepRagResult:
    """Multi-hop orchestration. Pure: `generate` (normal answer flow), `retrieve`
    (per-sub-question) and `llm` (decompose + compare) are injected.

    `force=False` (adaptive): single-shot first, escalate only if the coverage
    verdict is `partial`. `force=True`: always escalate regardless of the verdict —
    the per-question manual override. Even when forced the compare-guard still keeps
    single-shot if the deep answer isn't substantially better, so forcing can't
    produce a worse answer, only extra work.
    """
    ss = generate(query, selected_docs=None, want_verdict=True)
    # Escalation signals — ONLY these three mark the single-shot inadequate:
    #   1. the model's own verdict already said "partial",
    #   2. the answer is ungrounded (cited nothing), or
    #   3. it self-reports confidence below DEEP_RAG_MIN_CONFIDENCE.
    # Any of them forces coverage="partial" so it escalates (the model's self-reported
    # "complete" alone is not trusted).
    verdict_partial = ss.coverage == "partial"
    no_citations = not ss.citations
    low_confidence = _below_confidence_threshold(ss.answer)
    if verdict_partial or no_citations or low_confidence:
        ss.coverage = "partial"

    if ss.coverage != "partial" and not force:
        # complete + grounded + confident + not forced -> single-shot is enough.
        return _result(ss, escalated=False, source="single_shot", coverage=ss.coverage)

    # Why we escalated (recorded on the trace): first applicable signal.
    if verdict_partial:
        reason = "verdict_partial"
    elif no_citations:
        reason = "no_citations"
    elif low_confidence:
        reason = "low_confidence"
    else:
        reason = "forced"

    # Escalate. Fail-open: any error below returns the single-shot answer.
    try:
        sub_questions = decompose(
            query, ss.missing, llm, max_subquestions, history=history, subject=subject
        )
        # Curate the per-sub-question chunks; the generator answers over exactly these
        # (SearchTool selected_docs), so no retrieval/relevance-filter runs on synthesis.
        collated = _collate(sub_questions, retrieve)
        mh = generate(query, selected_docs=collated, want_verdict=False)

        def _pick(gen: _Gen, source: str) -> DeepRagResult:
            return _result(
                gen,
                escalated=True,
                source=source,
                coverage=ss.coverage,
                sub_questions=sub_questions,
                escalation_reason=reason,
            )

        # WINNING REQUIRES GROUNDING. An answer is "grounded" only if it has citations backed
        # by docs; an answer with no citations (or citations with no docs — which would render
        # as silence on Slack) is a punt, not a win. So:
        #   - both grounded    -> judge which is substantially better (SME/confidence stripped
        #                          before the compare so it weighs only substance);
        #   - only one grounded -> it wins outright (no judge — a flaky judge must never
        #                          resurrect an ungrounded punt over a grounded answer);
        #   - NEITHER grounded  -> no winner: surface the single-shot's honest punt (it still
        #                          carries its retrieved docs + the "insufficient confidence,
        #                          check SME" text) — never dress an ungrounded deep answer up
        #                          as the answer.
        ss_grounded = bool(ss.citations)
        mh_grounded = (
            bool(mh.citations)
            and bool(mh.docs)
            and bool(mh.answer and mh.answer.strip())
        )
        if mh_grounded and ss_grounded:
            keep_refined = refined_is_better(query, ss.answer, mh.answer, llm)
            return _pick(mh, "multi_hop") if keep_refined else _pick(ss, "single_shot")
        if mh_grounded:
            return _pick(mh, "multi_hop")
        # ss grounded (its cited answer) OR neither grounded (its honest punt) -> single-shot;
        # either way an ungrounded deep answer is never surfaced.
        return _pick(ss, "single_shot")
    except Exception:
        logger.exception("deep_rag escalation failed; falling back to single-shot")
        return _result(
            ss,
            escalated=True,
            source="single_shot",
            coverage=ss.coverage,
            escalation_reason=reason,
        )


_PROTECTED_SET = {
    DocumentSource._value2member_map_[s]
    for s in PROTECTED_SOURCES
    if s in DocumentSource._value2member_map_
}


def _section_source(section: Any) -> Any:
    src = getattr(section, "source_type", None)
    if src is not None:
        return src
    center = getattr(section, "center_chunk", None)
    return getattr(center, "source_type", None) if center is not None else None


def _topk_reserving_protected(sections: list[Any], k: int) -> list[Any]:
    """Top-k by score, but guarantee up to SOURCE_DIVERSITY_RESERVED_SLOTS PROTECTED_SOURCES
    sections survive into the result — parity with the normal answer path's doc-pruning, so
    curated sources (web/KB/OutSystems/highspot) reach the final synthesis even when pure
    score-truncation would drop them. `sections` must be pre-sorted by score desc."""
    if len(sections) <= k:
        return sections
    reserve = max(0, SOURCE_DIVERSITY_RESERVED_SLOTS)
    if reserve <= 0 or not _PROTECTED_SET:
        return sections[:k]
    guaranteed = [s for s in sections if _section_source(s) in _PROTECTED_SET][:reserve]
    if not guaranteed:
        return sections[:k]
    gid = {id(s) for s in guaranteed}
    rest = [s for s in sections if id(s) not in gid]
    merged = guaranteed + rest[: max(0, k - len(guaranteed))]
    return sorted(
        merged, key=lambda s: (getattr(s, "score", 0.0) or 0.0), reverse=True
    )[:k]


def _cap_protected_docs(sections: list[Any], max_docs: int) -> list[Any]:
    """Keep every non-PROTECTED_SOURCES section plus the protected sections belonging to
    at most `max_docs` distinct protected DOCUMENTS (ranked by their best chunk score).
    Preserves input order so [n] citation numbering stays stable. No-op when max_docs<0.
    """
    if not _PROTECTED_SET or max_docs < 0:
        return sections
    best: dict[Any, float] = {}
    for s in sections:
        if _section_source(s) in _PROTECTED_SET:
            did = getattr(s, "document_id", None)
            score = getattr(s, "score", 0.0) or 0.0
            if did not in best or score > best[did]:
                best[did] = score
    keep_docs = {
        d
        for d, _ in sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:max_docs]
    }
    return [
        s
        for s in sections
        if _section_source(s) not in _PROTECTED_SET
        or getattr(s, "document_id", None) in keep_docs
    ]


def _persona_retriever(
    user: User | None,
    persona: Persona,
    llm: LLM,
    fast_llm: LLM,
    db_session: Session,
    bypass_acl: bool = False,
) -> RetrieveFn:
    """Retrieval via SearchPipeline scoped to the persona, for the multi-hop
    per-sub-question step. Enforces the REAL user's ACL by default (web chat).
    `bypass_acl=True` only for surfaces that manage access differently — e.g. the Slack
    bot, which retrieves with user=None like its normal answer flow. Never bypass in the
    web request path."""

    def _one(query: str) -> list[Any]:
        request = SearchRequest(query=query, persona=persona)
        pipeline = SearchPipeline(
            search_request=request,
            user=user,
            llm=llm,
            fast_llm=fast_llm,
            db_session=db_session,
            bypass_acl=bypass_acl,
        )
        # reranked (post-rerank) sections, to match the single-shot's SearchTool retrieval.
        return list(pipeline.reranked_sections)

    def retrieve(query: str, k: int) -> list[Any]:
        # Version-normalized retrieval shared with the normal path (SearchTool); see
        # danswer.search.versioning.version_merged_sections. Then reserve slots for
        # PROTECTED_SOURCES so curated sources survive the top-k cut into the final
        # synthesis (parity with the normal answer path's doc-pruning).
        merged = version_merged_sections(_one, query)
        return _topk_reserving_protected(merged, k)

    return retrieve


def _thread_messages(question: str, history: str) -> list[ThreadMessage]:
    """The one-shot request thread: prior conversation (if any) as a leading message so
    the normal flow rephrases the question in context, then the current question last.
    """
    messages: list[ThreadMessage] = []
    if history and history.strip():
        messages.append(
            ThreadMessage(message=history.strip(), sender=None, role=MessageType.USER)
        )
    messages.append(ThreadMessage(message=question, sender=None, role=MessageType.USER))
    return messages


def _make_generate(
    persona: Persona,
    user: User | None,
    db_session: Session,
    history: str,
    bypass_acl: bool,
    danswerbot_flow: bool,
    verdict_llm: LLM | None = None,
) -> GenerateFn:
    """Build the real answer generator: each call runs the normal one-shot answer flow
    (persona prompt, citations, quotes, reflexion, doc-pruning, authoritative footer) —
    with NOTHING added to the answer prompt, so the answer is byte-identical to the
    normal flow's (a trailing self-assessment suffix measurably flattened the answer's
    markdown into plain prose). `want_verdict` instead runs a SEPARATE post-generation
    judge call (`verdict_llm`, the persona's fast LLM) over the finished question+answer
    for the coverage gate; fail-OPEN to "complete" on any judge error.
    `selected_docs` answers over pre-curated chunks instead of retrieving (multi-hop).
    """

    def generate(
        question: str,
        *,
        selected_docs: list[Any] | None = None,
        want_verdict: bool = False,
    ) -> _Gen:
        # `selected_docs` arrives as curated retrieval sections (multi-hop); convert to
        # the LlmDoc shape SearchTool.selected_docs expects here, so the orchestrator
        # stays free of that dependency.
        llm_docs: list[LlmDoc] | None = (
            [llm_doc_from_inference_section(s) for s in selected_docs]
            if selected_docs is not None
            else None
        )
        query_req = DirectQARequest(
            messages=_thread_messages(question, history),
            prompt_id=None,  # -> persona's own prompt (parity with the persona)
            persona_id=persona.id,
        )
        resp = get_search_answer(
            query_req=query_req,
            user=user,
            max_document_tokens=None,
            max_history_tokens=None,
            db_session=db_session,
            use_citations=True,
            bypass_acl=bypass_acl,
            danswerbot_flow=danswerbot_flow,
            selected_docs=llm_docs,
            # A legitimate low-confidence / SME-deferral answer never cites, so cap the
            # no-citation retry loop low for deep RAG (vs the default 5).
            max_citation_attempts=2,
        )
        coverage, missing = "complete", []
        if want_verdict and verdict_llm is not None:
            # External coverage judge (same ---VERDICT--- contract the old in-answer
            # suffix used, parsed by the same splitter). Fail-OPEN: judge trouble must
            # never break answering — worst case we just don't escalate.
            try:
                verdict_out = _invoke(
                    verdict_llm,
                    VERDICT_JUDGE_PROMPT.format(
                        question=question, answer=(resp.answer or "")
                    ),
                )
                _, coverage, missing = _split_verdict(
                    f"{_VERDICT_MARKER}{verdict_out}"
                    if _VERDICT_MARKER not in verdict_out
                    else verdict_out
                )
            except Exception:
                logger.exception("deep_rag verdict judge failed; assuming complete")
        gen = _to_gen(resp, coverage, missing)
        if selected_docs:
            # NON-EMPTY curated list: get_search_answer answered over exactly these
            # (SearchTool's specified-sections path) and its QADocsResponse carries no
            # top_documents (top_sections=[] there), so surface the curated sections we fed
            # in as the result docs — their document_ids back the answer's [n] citations so
            # the caller can persist + map them. Dedup by document_id (one doc can
            # contribute several collated chunks) so the Sources block shows each doc once.
            #
            # TRUTHY (not `is not None`) on purpose: SearchTool gates the specified-sections
            # path on `if self.selected_docs:`, so an EMPTY curated list (decompose/collate
            # surfaced nothing) makes it fall back to a NORMAL retrieval — that fresh answer
            # already has its own top_documents. Overwriting gen.docs with the empty list
            # there stripped the docs off a real, cited answer -> Slack saw 0 docs and went
            # silent. Skip the override when empty and keep the freshly-retrieved docs.
            seen_docs: set[Any] = set()
            unique_sections: list[Any] = []
            for s in selected_docs:
                did = getattr(s, "document_id", None)
                if did not in seen_docs:
                    seen_docs.add(did)
                    unique_sections.append(s)
            gen.docs = chunks_or_sections_to_search_docs(unique_sections)
        return gen

    return generate


def run_deep_rag(
    query: str,
    user: User | None,
    persona: Persona,
    db_session: Session,
    max_subquestions: int = DEFAULT_MAX_SUBQUESTIONS,
    history: str = "",
    force: bool = False,
    bypass_acl: bool = False,
    danswerbot_flow: bool = False,
) -> DeepRagResult:
    """Entry point: wire the normal answer flow + persona-scoped retriever + LLM into
    the deep_rag loop.

    `history` is the serialized prior conversation (empty for the first turn), so
    follow-up questions decompose/answer in context. `force=True` always escalates
    (the web manual override); `force=False` is verdict-gated (adaptive — used by
    Slack). `bypass_acl=True` for surfaces like the Slack bot (see _persona_retriever);
    `danswerbot_flow=True` for the Slack surface (token budgeting parity).
    """
    llm, fast_llm = get_llms_for_persona(persona)
    retrieve = _persona_retriever(
        user, persona, llm, fast_llm, db_session, bypass_acl=bypass_acl
    )
    generate = _make_generate(
        persona,
        user,
        db_session,
        history,
        bypass_acl,
        danswerbot_flow,
        verdict_llm=fast_llm,
    )
    subject = " — ".join(
        part.strip()
        for part in (persona.display_name or persona.name, persona.description)
        if part and part.strip()
    )
    return deep_rag_answer(
        query,
        generate,
        retrieve,
        llm,
        max_subquestions=max_subquestions,
        history=history,
        force=force,
        subject=subject,
    )
