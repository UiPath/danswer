"""Unit tests for the assistant router's pure logic.

The router asks a fast LLM, in one call, to pick the best assistant for a
question and return JSON {"persona_id": N|null, "confidence": x}. The parsing
must: extract the object amid prose, validate the id is in the catalog, honor a
confidence threshold, and FAIL OPEN (persona_id=None -> caller uses the
all-source fallback) on anything unparseable. `build_router_catalog` must prefer
routing_instructions, fall back to description, and skip entries with neither.
"""
from types import SimpleNamespace

from danswer.secondary_llm_flows.assistant_router import build_router_catalog
from danswer.secondary_llm_flows.assistant_router import keyword_route
from danswer.secondary_llm_flows.assistant_router import parse_route_response
from danswer.secondary_llm_flows.assistant_router import RouterCatalogEntry


def _persona(
    persona_id: int,
    description: str = "",
    routing: str | None = None,
    keywords: str | None = None,
):
    return SimpleNamespace(
        id=persona_id,
        name=f"persona-{persona_id}",
        description=description,
        routing_instructions=routing,
        routing_keywords=keywords,
    )


# --- build_router_catalog ---------------------------------------------------


def test_catalog_prefers_routing_instructions_over_description() -> None:
    catalog = build_router_catalog(
        [_persona(1, description="short desc", routing="exhaustive routing guide")]
    )
    assert len(catalog) == 1
    assert catalog[0].persona_id == 1
    assert catalog[0].routing_text == "exhaustive routing guide"


def test_catalog_falls_back_to_description_when_routing_blank() -> None:
    catalog = build_router_catalog(
        [
            _persona(1, description="orchestrator help", routing=None),
            _persona(2, description="apps help", routing="   "),  # whitespace-only
        ]
    )
    assert {e.persona_id: e.routing_text for e in catalog} == {
        1: "orchestrator help",
        2: "apps help",
    }


def test_catalog_skips_entries_with_no_routing_text() -> None:
    catalog = build_router_catalog(
        [_persona(1, description="", routing=None), _persona(2, description="real")]
    )
    assert [e.persona_id for e in catalog] == [2]


# --- parse_route_response ---------------------------------------------------

VALID = {1, 2, 3}


def test_parses_clean_object() -> None:
    r = parse_route_response('{"persona_id": 2, "confidence": 0.9}', VALID)
    assert r.persona_id == 2
    assert r.confidence == 0.9


def test_parses_object_amid_prose() -> None:
    r = parse_route_response(
        'Best match: {"persona_id": 3, "confidence": 0.8}. Hope that helps!', VALID
    )
    assert r.persona_id == 3


def test_null_persona_id_falls_back() -> None:
    r = parse_route_response('{"persona_id": null, "confidence": 0.2}', VALID)
    assert r.persona_id is None
    assert r.confidence == 0.2


def test_id_not_in_catalog_falls_back() -> None:
    r = parse_route_response('{"persona_id": 99, "confidence": 0.95}', VALID)
    assert r.persona_id is None


def test_low_confidence_falls_back_even_with_valid_id() -> None:
    r = parse_route_response(
        '{"persona_id": 1, "confidence": 0.3}', VALID, min_confidence=0.5
    )
    assert r.persona_id is None
    assert r.confidence == 0.3


def test_confidence_at_threshold_is_kept() -> None:
    r = parse_route_response(
        '{"persona_id": 1, "confidence": 0.5}', VALID, min_confidence=0.5
    )
    assert r.persona_id == 1


def test_bool_persona_id_rejected() -> None:
    # JSON `true` parses to Python True (an int subclass) — must not be treated as id 1.
    r = parse_route_response('{"persona_id": true, "confidence": 0.9}', VALID)
    assert r.persona_id is None


def test_confidence_clamped() -> None:
    assert parse_route_response('{"persona_id": 1, "confidence": 5}', VALID).confidence == 1.0
    assert (
        parse_route_response('{"persona_id": 1, "confidence": -2}', VALID).confidence
        == 0.0
    )


def test_single_quoted_python_dict_parsed() -> None:
    # The gateway model returns a Python dict literal (single quotes) in a
    # ```json fence — json.loads rejects it; ast.literal_eval fallback handles it.
    r = parse_route_response(
        "```json\n{'persona_id': 2, 'confidence': 1.0}\n```", VALID
    )
    assert r.persona_id == 2
    assert r.confidence == 1.0


def test_single_quoted_null_persona() -> None:
    r = parse_route_response("{'persona_id': None, 'confidence': 0.1}", VALID)
    assert r.persona_id is None


def test_unparseable_fails_open() -> None:
    assert parse_route_response("no json here", VALID).persona_id is None
    assert parse_route_response("", VALID).persona_id is None
    assert parse_route_response("{not valid json}", VALID).persona_id is None


def test_missing_confidence_treated_as_zero() -> None:
    r = parse_route_response('{"persona_id": 1}', VALID, min_confidence=0.5)
    assert r.persona_id is None
    assert r.confidence == 0.0


# --- keyword_route (deterministic pre-route) --------------------------------

def _entry(pid, name, keywords):
    return RouterCatalogEntry(
        persona_id=pid, name=name, routing_text="x", keywords=keywords
    )


_KW_CATALOG = [
    _entry(1, "AutomationSuite", ["automation suite", "as environment", "aks deployment", "eks deployment"]),
    _entry(2, "Orchestrator", ["orchestrator"]),
    _entry(3, "NoKeywords", []),
]


def test_keyword_route_matches_case_insensitively() -> None:
    assert keyword_route("AUTOMATION SUITE install on openshift", _KW_CATALOG).persona_id == 1
    assert keyword_route("migration to Unified on their AS Environment", _KW_CATALOG).persona_id == 1
    assert keyword_route("is AKS deployment supported", _KW_CATALOG).persona_id == 1


def test_keyword_route_none_when_no_match() -> None:
    # falls through to the LLM router (purely additive)
    assert keyword_route("what is the weather today", _KW_CATALOG) is None
    assert keyword_route("", _KW_CATALOG) is None


def test_keyword_route_longest_match_wins() -> None:
    cat = [
        _entry(1, "AS", ["as"]),
        _entry(2, "AutomationSuite", ["automation suite"]),
    ]
    # "automation suite" (longer) beats the substring "as"
    assert keyword_route("automation suite sizing", cat).persona_id == 2


def test_build_catalog_parses_keywords() -> None:
    cat = build_router_catalog([_persona(1, description="d", keywords="Foo, Bar Baz , ")])
    assert cat[0].keywords == ["foo", "bar baz"]  # trimmed + lowercased, blanks dropped
