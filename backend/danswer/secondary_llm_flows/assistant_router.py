"""Auto-route a question to the most relevant assistant (persona).

Powers the one-shot Search tab so users don't pick an assistant manually. Given
the question + a catalog of the user's accessible assistants (name + the
router-only `routing_instructions`, falling back to `description`), ONE fast-LLM
call returns the best persona id + a confidence.

Design notes:
- **ACL is the caller's job.** `build_router_catalog` only ever sees the list it
  is handed — callers MUST pass the user's accessible, visible, non-Slack
  personas. The /search/auto endpoint additionally re-checks the chosen persona
  with `get_persona_by_id(..., user=)` on the trusted side before answering.
- **Fail-OPEN.** Any ambiguity, parse failure, or LLM error returns
  `persona_id=None`; the caller then uses the all-source default persona, so the
  search box never dead-ends.
- **Pure, testable core.** `parse_route_response` does the parsing/validation
  with no I/O so routing logic is unit-testable without an LLM.
"""
import ast
import json
import re
from typing import Protocol

from pydantic import BaseModel

from danswer.llm.interfaces import LLM
from danswer.llm.utils import message_to_string
from danswer.utils.logger import setup_logger

logger = setup_logger()


class RoutableAssistant(Protocol):
    """Structural type for anything the router can catalog — satisfied by both
    the Persona ORM model and PersonaSnapshot (the cached form). The endpoint
    feeds cached PersonaSnapshots; the unit tests feed lightweight stand-ins."""

    id: int
    name: str
    description: str
    routing_instructions: str | None
    routing_keywords: str | None
    routing_intents: str | None


# Below this, treat the route as "not confident" -> fall back to all-source.
DEFAULT_MIN_CONFIDENCE = 0.5


class RouterCatalogEntry(BaseModel):
    persona_id: int
    name: str
    routing_text: str
    # Deterministic keyword overrides (lowercased phrases). Empty => no override.
    keywords: list[str] = []
    # Semantic intent exemplars (natural-language phrases, one per line in the DB).
    # Used by the embedding pre-route; empty => this assistant opts out of it.
    intents: list[str] = []


class RouteResult(BaseModel):
    # The confident #1 pick, or None when the top is below the confidence
    # threshold (caller then treats it as "no single winner").
    persona_id: int | None
    confidence: float
    # The full best-first, validated, in-catalog ranking from the SAME router call
    # (up to N), INDEPENDENT of the confidence threshold. The caller answers over
    # the UNION of these assistants' document sets — so a slightly-wrong #1 doesn't
    # sink the answer. Empty for the deterministic keyword route.
    ranked_ids: list[int] = []


def _routing_text(persona: RoutableAssistant) -> str:
    """Router signal for a persona: the router-only `routing_instructions`, or
    `description` when that's blank (graceful fallback for un-curated assistants)."""
    instructions = (persona.routing_instructions or "").strip()
    if instructions:
        return instructions
    return (persona.description or "").strip()


def build_router_catalog(
    personas: list[RoutableAssistant],
) -> list[RouterCatalogEntry]:
    """Build the routable-assistant catalog from an ALREADY ACL-filtered list.

    Caller passes the user's accessible + visible + non-Slack personas. Entries
    with no routing text (nothing for the LLM to match on) are skipped."""
    catalog: list[RouterCatalogEntry] = []
    for persona in personas:
        text = _routing_text(persona)
        keywords = [
            kw.strip().lower()
            for kw in (persona.routing_keywords or "").split(",")
            if kw.strip()
        ]
        intents = parse_intents(getattr(persona, "routing_intents", None))
        # An entry with keywords/intents but no routing_text is still useful (a
        # pre-route doesn't need LLM text), so keep it if it has any signal.
        if not text and not keywords and not intents:
            continue
        catalog.append(
            RouterCatalogEntry(
                persona_id=persona.id,
                name=persona.name,
                routing_text=text,
                keywords=keywords,
                intents=intents,
            )
        )
    return catalog


def parse_intents(raw: str | None) -> list[str]:
    """Newline-separated intent phrases -> cleaned list (trimmed, blanks dropped)."""
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


_WORD_RE = re.compile(r"[a-z0-9]+")
# A short keyword token must match a WHOLE word; a longer one (>= this) may also
# match as a start-anchored prefix, so stems like "expir"/"licens" hit
# "expired"/"licensing" without the substring traps (e.g. "form" != "perform").
_PREFIX_MIN_LEN = 4


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def _keyword_tokens(keyword: str) -> list[str]:
    return _WORD_RE.findall((keyword or "").lower())


def _token_present(token: str, words: set[str]) -> bool:
    if token in words:
        return True
    if len(token) >= _PREFIX_MIN_LEN:
        return any(w.startswith(token) for w in words)
    return False


def keyword_route(
    question: str, catalog: list[RouterCatalogEntry]
) -> RouteResult | None:
    """Deterministic pre-route: route straight to an assistant — BEFORE the LLM
    router — when the question matches one of its configured keywords. Returns None
    when nothing matches (caller then runs the LLM router), so this is additive.

    An UNQUOTED keyword is FUZZY: it is split into words and matches when EVERY one
    of those words appears in the question, in any order/position. Each word matches
    a question word exactly, or (for words >= 4 chars) as a start-anchored prefix
    (so "sla expir" hits "the SLA on that task expired", "round robin" hits
    "assign round-robin to the group"). A single-word keyword reduces to
    word-present.

    A "QUOTED" keyword matches only as an EXACT CONTIGUOUS phrase (case-insensitive
    substring) — use it when the words are only meaningful adjacent, e.g. an
    abbreviation that is also a common word: "as environment" (AS = Automation
    Suite) must appear together, not as "as" and "environment" scattered.

    On multiple matches the MOST SPECIFIC keyword wins — more words first, then
    more characters — with persona id as a stable tiebreak. confidence=1.0."""
    words = _words(question)
    if not words:
        return None
    q_lower = question.lower()
    best_key: tuple[int, int, int] | None = None  # (num_words, char_len, -persona_id)
    best_id: int | None = None
    for entry in catalog:
        for kw in entry.keywords:
            kw = kw.strip()
            if len(kw) >= 2 and kw[0] == '"' and kw[-1] == '"':
                # Quoted -> exact contiguous phrase match.
                phrase = kw[1:-1].strip()
                tokens = _keyword_tokens(phrase)
                if not phrase or phrase not in q_lower:
                    continue
            else:
                # Unquoted -> fuzzy: all words present, any order/position.
                tokens = _keyword_tokens(kw)
                if not tokens or not all(_token_present(t, words) for t in tokens):
                    continue
            key = (len(tokens), len("".join(tokens)), -entry.persona_id)
            if best_key is None or key > best_key:
                best_key = key
                best_id = entry.persona_id
    if best_id is None:
        return None
    logger.info("assistant router: keyword override -> persona_id=%s", best_id)
    return RouteResult(persona_id=best_id, confidence=1.0)


# Minimum LLM-reported confidence for the intent (phrase) pre-route to fire. High,
# because a fire is a deterministic route that skips the instruction router.
DEFAULT_INTENT_MIN_CONFIDENCE = 0.8

_INTENT_PROMPT = """\
You decide whether a user's question clearly matches ONE assistant's intents.
Each assistant lists example intent phrases — the kinds of questions it handles.

USER QUESTION:
{question}

ASSISTANTS AND THEIR INTENT PHRASES:
{catalog}

If the question clearly matches one assistant's intents, respond with ONLY a JSON \
object: {{"persona_id": <that assistant's id>, "confidence": <a number 0 to 1>}}. \
If it does not clearly match any assistant's intents, respond \
{{"persona_id": null, "confidence": 0}}. Match on MEANING, not surface word overlap — \
only pick an assistant when you are confident the question is really about its intents.
"""


def _render_intents(entries: list[RouterCatalogEntry]) -> str:
    return "\n\n".join(
        f"[id={e.persona_id}] {e.name}\n" + "\n".join(f"- {p}" for p in e.intents)
        for e in entries
        if e.intents
    )


def intent_route(
    question: str,
    catalog: list[RouterCatalogEntry],
    llm: LLM,
    min_confidence: float = DEFAULT_INTENT_MIN_CONFIDENCE,
) -> RouteResult | None:
    """Semantic pre-route via an LLM over assistants' intent phrases — BETWEEN the
    keyword pre-route and the LLM instruction router. Sends every assistant's
    routing_intents (in ONE call) and asks which assistant the question clearly
    matches. Fires only above `min_confidence` (high — it's a deterministic route);
    returns None otherwise so the caller falls through to the instruction router.
    Uses the LLM's meaning-understanding, which (unlike cosine/lexical phrase
    matching) rejects surface word-overlap false matches. Fail-OPEN on empty/error."""
    entries = [e for e in catalog if e.intents]
    if not entries or not question.strip():
        return None
    prompt = _INTENT_PROMPT.format(
        question=question.strip(), catalog=_render_intents(entries)
    )
    valid_ids = {e.persona_id for e in entries}
    try:
        raw = message_to_string(llm.invoke(prompt))
    except Exception as e:
        logger.warning("assistant router: intent LLM call failed, skipping: %s", e)
        return None
    result = parse_route_response(raw, valid_ids, min_confidence=min_confidence)
    if result.persona_id is None:
        return None
    logger.info(
        "assistant router: intent match -> persona_id=%s confidence=%.2f",
        result.persona_id,
        result.confidence,
    )
    return RouteResult(persona_id=result.persona_id, confidence=result.confidence)


_ROUTER_PROMPT = """\
You are a router that ranks the assistants best suited to answer a user's question.
Each assistant covers a specific product area or topic.

USER QUESTION:
{question}

AVAILABLE ASSISTANTS:
{catalog}

Rank the assistants whose scope best matches the question, MOST CONFIDENT FIRST, up \
to 3. Respond with ONLY a JSON object: \
{{"ranked": [{{"persona_id": <id>, "confidence": <a number from 0 to 1>}}, ...]}}.
Include only assistants that plausibly fit. Return an empty list if the question is \
generic, spans many areas, or no assistant clearly fits — do not guess.
"""


def _render_catalog(catalog: list[RouterCatalogEntry]) -> str:
    return "\n\n".join(
        f"[id={entry.persona_id}] {entry.name}\n{entry.routing_text}"
        for entry in catalog
    )


def _parse_ranked_item(item: object, valid_ids: set[int]) -> tuple[int, float] | None:
    """Validate one {persona_id, confidence} entry -> (id, clamped_conf) or None."""
    if not isinstance(item, dict):
        return None
    rid = item.get("persona_id")
    # bool is an int subclass — reject it explicitly.
    if not isinstance(rid, int) or isinstance(rid, bool) or rid not in valid_ids:
        return None
    try:
        conf = float(item.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    return rid, max(0.0, min(1.0, conf))


def parse_route_response(
    raw: str,
    valid_ids: set[int],
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    max_ranked: int = 3,
) -> RouteResult:
    """Parse the router LLM's JSON into a validated RouteResult. Pure / no I/O.

    Expects the ranked form {"ranked": [{"persona_id", "confidence"}, ...]} but
    also accepts the legacy single object {"persona_id", "confidence"}. Returns
    `persona_id` = the best in-catalog pick (None, i.e. fail-open, when the response
    is unparseable, names no valid id, or the top is below the confidence
    threshold) and `ranked_ids` = the full best-first in-catalog list (up to
    `max_ranked`), independent of the threshold, for union-scope answering.

    Fail-OPEN on anything unparseable."""
    # Grab the outermost {...}; DOTALL + greedy so the nested "ranked" list (which
    # contains its own {} objects) is captured whole, not truncated at the first }.
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return RouteResult(persona_id=None, confidence=0.0)
    blob = match.group(0)
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        # The gateway model (gpt-4.1-mini) often returns a PYTHON dict literal
        # with single quotes (e.g. {'persona_id': 35, 'confidence': 1.0}), which
        # json.loads rejects. ast.literal_eval safely parses literals.
        try:
            data = ast.literal_eval(blob)
        except (ValueError, SyntaxError, TypeError):
            return RouteResult(persona_id=None, confidence=0.0)
    if not isinstance(data, dict):
        return RouteResult(persona_id=None, confidence=0.0)

    ranked = data.get("ranked")
    if not isinstance(ranked, list):
        ranked = [data]  # legacy single-object format

    # Validated (id, confidence), best-first, deduped, in-catalog only.
    parsed: list[tuple[int, float]] = []
    seen: set[int] = set()
    for item in ranked:
        entry = _parse_ranked_item(item, valid_ids)
        if entry is None or entry[0] in seen:
            continue
        seen.add(entry[0])
        parsed.append(entry)

    if not parsed:
        return RouteResult(persona_id=None, confidence=0.0)

    ranked_ids = [rid for rid, _ in parsed[:max_ranked]]
    top_id, top_conf = parsed[0]
    # persona_id (the confident #1) drives display/identity; ranked_ids (always the
    # full list) drives the union scope. A low-confidence top => persona_id=None but
    # ranked_ids still populated, so the ambiguous case widens scope instead of
    # collapsing to the all-source fallback.
    return RouteResult(
        persona_id=top_id if top_conf >= min_confidence else None,
        confidence=top_conf,
        ranked_ids=ranked_ids,
    )


def route_question(
    question: str,
    catalog: list[RouterCatalogEntry],
    llm: LLM,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    top_n: int = 3,
) -> RouteResult:
    """One fast-LLM call -> the best persona id + the top-N ranking (or None to
    fall back).

    Fail-OPEN on empty input / LLM error -> persona_id=None."""
    if not catalog or not question.strip():
        return RouteResult(persona_id=None, confidence=0.0)

    prompt = _ROUTER_PROMPT.format(
        question=question.strip(), catalog=_render_catalog(catalog)
    )
    valid_ids = {entry.persona_id for entry in catalog}
    try:
        raw = message_to_string(llm.invoke(prompt))
    except Exception as e:
        logger.warning("assistant router: LLM call failed, falling back: %s", e)
        return RouteResult(persona_id=None, confidence=0.0)

    result = parse_route_response(
        raw, valid_ids, min_confidence=min_confidence, max_ranked=top_n
    )
    logger.info(
        "assistant router: question=%r -> persona_id=%s confidence=%.2f ranked=%s",
        question[:80],
        result.persona_id,
        result.confidence,
        result.ranked_ids,
    )
    return result
