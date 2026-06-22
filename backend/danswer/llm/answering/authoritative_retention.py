"""Verify-then-retain authoritative citations.

Citations are the LLM's own output, and it inconsistently cites curated /
authoritative sources even when they're promoted to the front of the prompt
(confirmed across chat + Slack, multiple runs). This module adds a post-generation
step that is:

- **additive**  — the LLM's own inline citations are untouched;
- **deduped**   — only considers authoritative docs the LLM did NOT cite, deduped
                  by document_id (the same page often appears as several chunks);
- **honest**    — appends a doc only if one batched LLM call confirms it actually
                  supports a statement in the answer (fail-closed on any error);
- **bounded**   — at most ONE extra LLM call, and only when an uncited authoritative
                  doc is present in the context.

Supporting docs are appended as a small "Authoritative sources" markdown footer,
which renders in both the chat UI and Slack.
"""
import json
import re

from danswer.chat.models import LlmDoc
from danswer.configs.chat_configs import PROTECTED_SOURCES
from danswer.llm.interfaces import LLM
from danswer.llm.utils import message_to_string
from danswer.utils.logger import setup_logger

logger = setup_logger()


_VERIFY_PROMPT = """\
You are checking which authoritative reference documents actually support an answer \
that was already written.

ANSWER:
{answer}

CANDIDATE AUTHORITATIVE DOCUMENTS:
{docs}

For EACH candidate, decide whether it directly supports at least one factual \
statement in the ANSWER above — being on the same topic is NOT enough. Respond with \
ONLY a JSON array of the numbers of the documents that genuinely support the answer \
(e.g. [1, 3]). If none do, respond with [].
"""


def _source_value(doc: LlmDoc) -> str:
    src = doc.source_type
    return (src.value if hasattr(src, "value") else str(src)).lower()


def select_authoritative_candidates(
    final_context_docs: list[LlmDoc],
    already_cited_doc_ids: set[str],
) -> list[LlmDoc]:
    """Authoritative-source docs that are in the prompt but the LLM did NOT cite.
    Deduped by document_id; only docs with a renderable link. Pure (no I/O)."""
    protected = set(PROTECTED_SOURCES)
    out: list[LlmDoc] = []
    seen: set[str] = set()
    for doc in final_context_docs:
        if _source_value(doc) not in protected:
            continue
        if doc.document_id in already_cited_doc_ids or doc.document_id in seen:
            continue
        if not doc.link:
            continue
        seen.add(doc.document_id)
        out.append(doc)
    return out


def parse_supporting_indices(raw: str, n: int) -> list[int]:
    """Parse the verify call's JSON array into 0-based indices in [0, n). Tolerant:
    returns [] if nothing parseable (fail-closed → append nothing)."""
    match = re.search(r"\[[^\[\]]*\]", raw)
    if not match:
        return []
    try:
        values = json.loads(match.group(0))
    except (ValueError, TypeError):
        return []
    out: list[int] = []
    for v in values:
        if isinstance(v, bool):
            continue
        if isinstance(v, int) and 1 <= v <= n:
            out.append(v - 1)
    return out


def verify_supporting_docs(
    answer: str, candidates: list[LlmDoc], llm: LLM, snippet_chars: int = 600
) -> list[LlmDoc]:
    """One batched LLM call: which candidates support the answer? Fail-closed."""
    if not candidates or not answer.strip():
        return []
    docs_str = "\n".join(
        f"{i + 1}. {d.semantic_identifier}: {d.content[:snippet_chars]}"
        for i, d in enumerate(candidates)
    )
    prompt = _VERIFY_PROMPT.format(answer=answer.strip(), docs=docs_str)
    try:
        raw = message_to_string(llm.invoke(prompt))
    except Exception as e:
        logger.warning("authoritative retention: verify call failed: %s", e)
        return []
    return [candidates[i] for i in parse_supporting_indices(raw, len(candidates))]


def build_authoritative_footer(docs: list[LlmDoc]) -> str:
    """Markdown footer of verified authoritative sources (renders in chat + Slack)."""
    if not docs:
        return ""
    lines = "\n".join(f"- [{d.semantic_identifier}]({d.link})" for d in docs)
    return f"\n\n**Authoritative sources:**\n{lines}"


def retained_authoritative_footer(
    answer: str,
    final_context_docs: list[LlmDoc],
    already_cited_doc_ids: set[str],
    llm: LLM,
) -> str:
    """candidates → verify → footer. Returns "" when there's nothing to add (and
    makes NO LLM call in that case)."""
    candidates = select_authoritative_candidates(
        final_context_docs, already_cited_doc_ids
    )
    if not candidates:
        return ""
    supporting = verify_supporting_docs(answer, candidates, llm)
    if supporting:
        logger.info(
            "authoritative retention: appended %d source(s): %s",
            len(supporting),
            [d.semantic_identifier for d in supporting],
        )
    return build_authoritative_footer(supporting)
