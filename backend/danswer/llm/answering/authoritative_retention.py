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
You are selecting authoritative reference documents to surface as sources for an \
answer to a user's QUESTION. For each candidate you are given the PASSAGE from that \
document that the search actually matched — judge from that passage.

QUESTION:
{question}

ANSWER GIVEN:
{answer}

CANDIDATE AUTHORITATIVE DOCUMENTS (matched passage shown):
{docs}

For EACH candidate, use its matched passage to decide whether the document genuinely \
helps answer THIS QUESTION — it must address the specific subject the question is \
about. Sharing a keyword, product, or service name is NOT enough: exclude a document \
that is really about a different feature, or about troubleshooting a specific error, \
when that is not what the question asks about. When in doubt, exclude. Respond with \
ONLY a JSON array of the numbers of the genuinely relevant documents (e.g. [1, 3]); \
if none qualify, respond with [].
"""


def _source_value(doc: LlmDoc) -> str:
    src = doc.source_type
    return (src.value if hasattr(src, "value") else str(src)).lower()


def select_authoritative_candidates(
    final_context_docs: list[LlmDoc],
    already_cited_doc_ids: set[str],
) -> list[LlmDoc]:
    """Authoritative (PROTECTED_SOURCES) docs in the prompt that the LLM did NOT
    cite — candidates for the relevance-verify step.

    We surface a relevant authoritative doc the LLM left out **even if it cited some
    other authoritative source** — citing one KB article shouldn't suppress a
    different, relevant docs/web page the answer also draws on. (We tried a tighter
    "skip if any authoritative was cited" gate to save the verify call, but it hid
    exactly these docs, so it was loosened.) Deduped by document_id; link required;
    pure (no I/O). Empty → no verify call."""
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
    answer: str,
    candidates: list[LlmDoc],
    llm: LLM,
    question: str = "",
    snippet_chars: int = 4000,
    max_attempts: int = 2,
) -> list[LlmDoc]:
    """One batched LLM call: which candidates are relevant authoritative references
    for the answer? The judgment is made against each doc's MATCHED PASSAGE (the
    retrieved chunk that scored against the query, i.e. LlmDoc.content) rather than a
    short title/prefix — this is the reliable signal for relevance, so we pass the
    full passage (capped generously). The candidate list is small (1-3 after the
    gate/dedupe), so the extra tokens are bounded. Retries once on a transient
    failure (the gateway non-streaming completion occasionally times out), then
    fails closed (returns [] → no footer) so we never append on a real error."""
    if not candidates or not answer.strip():
        return []
    docs_str = "\n\n".join(
        f"[{i + 1}] {d.semantic_identifier}\nMatched passage: {d.content[:snippet_chars]}"
        for i, d in enumerate(candidates)
    )
    prompt = _VERIFY_PROMPT.format(
        question=(question or "(not provided)").strip(), answer=answer.strip(), docs=docs_str
    )
    for attempt in range(1, max_attempts + 1):
        try:
            raw = message_to_string(llm.invoke(prompt))
            return [candidates[i] for i in parse_supporting_indices(raw, len(candidates))]
        except Exception as e:
            logger.warning(
                "authoritative retention: verify call failed (attempt %d/%d): %s",
                attempt,
                max_attempts,
                e,
            )
    return []


def build_authoritative_footer(docs: list[LlmDoc]) -> str:
    """Markdown footer of verified authoritative sources (renders in chat + Slack).

    Rendered as its own labelled block rather than merged into the numbered Sources
    cards: citation numbers ARE context positions and the LLM already owns the low
    ones, so injecting into that list either collides (de-duped away) or can't be
    placed at the top without renumbering the LLM's inline citations. A footer
    sidesteps that and surfaces the link unambiguously."""
    if not docs:
        return ""
    lines = "\n".join(f"- [{d.semantic_identifier}]({d.link})" for d in docs)
    return f"\n\n**Authoritative sources:**\n{lines}"


def retained_authoritative_footer(
    answer: str,
    final_context_docs: list[LlmDoc],
    already_cited_doc_ids: set[str],
    llm: LLM,
    question: str = "",
) -> str:
    """candidates → verify → footer. Returns "" (and makes NO LLM call) when there
    are no uncited authoritative candidates. `question` anchors the relevance check
    to what was actually asked (so docs that merely share a keyword/service with the
    answer are excluded)."""
    candidates = select_authoritative_candidates(
        final_context_docs, already_cited_doc_ids
    )
    if not candidates:
        return ""
    supporting = verify_supporting_docs(answer, candidates, llm, question=question)
    if supporting:
        logger.info(
            "authoritative retention: appended %d source(s): %s",
            len(supporting),
            [d.semantic_identifier for d in supporting],
        )
    return build_authoritative_footer(supporting)
