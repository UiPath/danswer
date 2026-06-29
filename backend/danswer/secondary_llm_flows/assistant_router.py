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

from pydantic import BaseModel

from danswer.db.models import Persona
from danswer.llm.interfaces import LLM
from danswer.llm.utils import message_to_string
from danswer.utils.logger import setup_logger

logger = setup_logger()


# Below this, treat the route as "not confident" -> fall back to all-source.
DEFAULT_MIN_CONFIDENCE = 0.5


class RouterCatalogEntry(BaseModel):
    persona_id: int
    name: str
    routing_text: str


class RouteResult(BaseModel):
    # None => no confident match; caller uses the all-source fallback persona.
    persona_id: int | None
    confidence: float


def _routing_text(persona: Persona) -> str:
    """Router signal for a persona: the router-only `routing_instructions`, or
    `description` when that's blank (graceful fallback for un-curated assistants)."""
    instructions = (persona.routing_instructions or "").strip()
    if instructions:
        return instructions
    return (persona.description or "").strip()


def build_router_catalog(personas: list[Persona]) -> list[RouterCatalogEntry]:
    """Build the routable-assistant catalog from an ALREADY ACL-filtered list.

    Caller passes the user's accessible + visible + non-Slack personas. Entries
    with no routing text (nothing for the LLM to match on) are skipped."""
    catalog: list[RouterCatalogEntry] = []
    for persona in personas:
        text = _routing_text(persona)
        if not text:
            continue
        catalog.append(
            RouterCatalogEntry(
                persona_id=persona.id, name=persona.name, routing_text=text
            )
        )
    return catalog


_ROUTER_PROMPT = """\
You are a router that picks the single best assistant to answer a user's question.
Each assistant covers a specific product area or topic.

USER QUESTION:
{question}

AVAILABLE ASSISTANTS:
{catalog}

Pick the ONE assistant whose scope best matches the question. Respond with ONLY a \
JSON object: {{"persona_id": <the id of the best assistant, or null if none clearly \
fits>, "confidence": <a number from 0 to 1 for how sure you are>}}.
Use null with a low confidence if the question is generic, spans many areas, or no \
assistant clearly fits — do not guess.
"""


def _render_catalog(catalog: list[RouterCatalogEntry]) -> str:
    return "\n\n".join(
        f"[id={entry.persona_id}] {entry.name}\n{entry.routing_text}"
        for entry in catalog
    )


def parse_route_response(
    raw: str,
    valid_ids: set[int],
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> RouteResult:
    """Parse the router LLM's JSON into a validated RouteResult. Pure / no I/O.

    Fail-OPEN: returns persona_id=None when the response is unparseable, names an
    id not in the catalog, or reports confidence below the threshold."""
    match = re.search(r"\{[^{}]*\}", raw)
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

    raw_conf = data.get("confidence")
    try:
        confidence = float(raw_conf)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    raw_id = data.get("persona_id")
    # bool is an int subclass — reject it explicitly.
    if not isinstance(raw_id, int) or isinstance(raw_id, bool):
        return RouteResult(persona_id=None, confidence=confidence)
    if raw_id not in valid_ids:
        return RouteResult(persona_id=None, confidence=confidence)
    if confidence < min_confidence:
        return RouteResult(persona_id=None, confidence=confidence)
    return RouteResult(persona_id=raw_id, confidence=confidence)


def route_question(
    question: str,
    catalog: list[RouterCatalogEntry],
    llm: LLM,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> RouteResult:
    """One fast-LLM call -> the best persona id (or None to fall back).

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

    result = parse_route_response(raw, valid_ids, min_confidence=min_confidence)
    logger.info(
        "assistant router: question=%r -> persona_id=%s confidence=%.2f",
        question[:80],
        result.persona_id,
        result.confidence,
    )
    return result
