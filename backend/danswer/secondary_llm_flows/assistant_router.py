"""Deterministic keyword pre-route + shared routing types for the Search tab.

The router pipeline is:
  1. `keyword_route` (here) — deterministic, no LLM. Fires only when a question
     matches an assistant's configured `routing_keywords`.
  2. kNN-over-Slack fallback (see `slack_knn_router`) — when no keyword matches.

The old LLM routing-instructions logic (an LLM ranking assistants by their
curated `routing_instructions` / `routing_intents`) has been removed in favor of
the self-maintaining kNN router; `RouteResult` is shared by both stages.

Design notes:
- **ACL is the caller's job.** `build_router_catalog` only ever sees the list it
  is handed — callers MUST pass the user's accessible, visible, non-Slack
  personas. The catalog's persona-id set also fences the kNN router (it can only
  route to an assistant present here).
- **Fail-OPEN.** No keyword match -> `keyword_route` returns None and the caller
  falls through to the kNN router; a dead-end never happens.
- **Pure, testable core.** No I/O here.
"""
import re
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel

from danswer.utils.logger import setup_logger

logger = setup_logger()


class RoutableAssistant(Protocol):
    """Structural type for anything the router can catalog — satisfied by both
    the Persona ORM model and PersonaSnapshot (the cached form). The endpoint
    feeds cached PersonaSnapshots; the unit tests feed lightweight stand-ins."""

    id: int
    name: str
    routing_keywords: str | None


class RouterCatalogEntry(BaseModel):
    persona_id: int
    name: str
    # Deterministic keyword overrides (lowercased phrases). Empty => no keyword
    # override for this assistant (it can still be reached via the kNN router).
    keywords: list[str] = []


class RouteResult(BaseModel):
    # The confident #1 pick, or None when no route was found (caller then uses the
    # all-source default persona).
    persona_id: int | None
    confidence: float
    # The full best-first ranking (up to N) for the "recommended assistants" UI.
    # Empty for the deterministic keyword route.
    ranked_ids: list[int] = []
    # True when the route was a close call — the kNN vote was below the confidence
    # threshold and needed the LLM tiebreak. The endpoint offers the side-by-side
    # compare (single pick vs. union of the top-N) only in this ambiguous case.
    ambiguous: bool = False


def build_router_catalog(
    personas: Sequence[RoutableAssistant],
) -> list[RouterCatalogEntry]:
    """Build the routable-assistant catalog from an ALREADY ACL-filtered list.

    Caller passes the user's accessible + visible + non-Slack personas. EVERY
    persona is included (its id fences the kNN router) — an assistant needs no
    keywords to be routable, since the kNN fallback routes without them."""
    catalog: list[RouterCatalogEntry] = []
    for persona in personas:
        keywords = [
            kw.strip().lower()
            for kw in (persona.routing_keywords or "").split(",")
            if kw.strip()
        ]
        catalog.append(
            RouterCatalogEntry(
                persona_id=persona.id,
                name=persona.name,
                keywords=keywords,
            )
        )
    return catalog


_WORD_RE = re.compile(r"[a-z0-9]+")
# A short keyword token must match a WHOLE word; a longer one (>= this) may also
# match as a start-anchored prefix, so stems like "expir"/"licens" hit
# "expired"/"licensing" without the substring traps (e.g. "form" != "perform").
_PREFIX_MIN_LEN = 4


def _keyword_tokens(keyword: str) -> list[str]:
    return _WORD_RE.findall((keyword or "").lower())


def _token_present(token: str, words: set[str]) -> bool:
    if token in words:
        return True
    if len(token) >= _PREFIX_MIN_LEN:
        return any(w.startswith(token) for w in words)
    return False


def _word_token_match(token: str, word: str) -> bool:
    """One position: exact word, or start-anchored prefix for tokens >= 4 chars."""
    return word == token or (len(token) >= _PREFIX_MIN_LEN and word.startswith(token))


def _is_contiguous(tokens: list[str], word_list: list[str]) -> bool:
    """True if the keyword's tokens appear as an adjacent, in-order run of words."""
    n = len(tokens)
    for i in range(len(word_list) - n + 1):
        if all(_word_token_match(tokens[j], word_list[i + j]) for j in range(n)):
            return True
    return False


def keyword_route(
    question: str, catalog: list[RouterCatalogEntry]
) -> RouteResult | None:
    """Deterministic pre-route: route straight to an assistant when the question
    matches one of its configured keywords. Returns None when nothing matches
    (caller then runs the kNN router), so this is additive.

    Keyword forms (all in the one comma-separated column):
    - FUZZY (unquoted, e.g. `task sla`): matches when EVERY word appears in the
      question, any order/position — exact, or a start-anchored prefix for words
      >= 4 chars (so `sla expir` hits "the SLA ... expired").
    - EXACT (quoted, e.g. `"as environment"`): matches only as a contiguous phrase.
      Use for abbreviations that are also common words (AS = Automation Suite).
    - ALWAYS-WINS (`!` prefix, e.g. `!automation suite` or `!"as environment"`):
      a priority flag — if it matches it outranks every non-priority match.

    Ranking among matches (highest wins):
      (priority, contiguous, exact-word-count, num_words, char_len, -persona_id)
    So an explicitly-tagged keyword wins first; otherwise a keyword whose words
    appear ADJACENT and EXACT beats one that only matched scattered / via prefix
    (e.g. `automation suite` verbatim beats `integration service` matched on the
    incidental words "integrations"/"services"). confidence=1.0."""
    word_list = _WORD_RE.findall((question or "").lower())
    words = set(word_list)
    if not words:
        return None
    q_lower = question.lower()
    best_key: tuple | None = None
    best_id: int | None = None
    for entry in catalog:
        for raw in entry.keywords:
            kw = raw.strip()
            priority = kw.startswith("!")
            if priority:
                kw = kw[1:].strip()
            if len(kw) >= 2 and kw[0] == '"' and kw[-1] == '"':
                # Quoted -> exact contiguous phrase match.
                phrase = kw[1:-1].strip()
                tokens = _keyword_tokens(phrase)
                if not phrase or phrase not in q_lower:
                    continue
                contiguous = True
            else:
                # Unquoted -> fuzzy: all words present, any order/position.
                tokens = _keyword_tokens(kw)
                if not tokens or not all(_token_present(t, words) for t in tokens):
                    continue
                contiguous = _is_contiguous(tokens, word_list)
            exact = sum(1 for t in tokens if t in words)
            key = (
                priority,
                contiguous,
                exact,
                len(tokens),
                len("".join(tokens)),
                -entry.persona_id,
            )
            if best_key is None or key > best_key:
                best_key = key
                best_id = entry.persona_id
    if best_id is None:
        return None
    logger.info("assistant router: keyword override -> persona_id=%s", best_id)
    return RouteResult(persona_id=best_id, confidence=1.0)
