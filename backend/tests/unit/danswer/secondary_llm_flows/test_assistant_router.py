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
from danswer.secondary_llm_flows.assistant_router import intent_route
from danswer.secondary_llm_flows.assistant_router import keyword_route
from danswer.secondary_llm_flows.assistant_router import parse_intents
from danswer.secondary_llm_flows.assistant_router import parse_route_response
from danswer.secondary_llm_flows.assistant_router import RouterCatalogEntry


def _persona(
    persona_id: int,
    description: str = "",
    routing: str | None = None,
    keywords: str | None = None,
    intents: str | None = None,
):
    return SimpleNamespace(
        id=persona_id,
        name=f"persona-{persona_id}",
        description=description,
        routing_instructions=routing,
        routing_keywords=keywords,
        routing_intents=intents,
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
    # A null id yields no valid candidate -> fail-open with no confidence/ranking.
    r = parse_route_response('{"persona_id": null, "confidence": 0.2}', VALID)
    assert r.persona_id is None
    assert r.confidence == 0.0
    assert r.ranked_ids == []


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


# --- parse_route_response: ranked top-N form --------------------------------


def test_parses_ranked_list() -> None:
    # New primary format: {"ranked": [...]} best-first. Top pick + full ranking.
    r = parse_route_response(
        '{"ranked": [{"persona_id": 2, "confidence": 0.9}, '
        '{"persona_id": 3, "confidence": 0.7}, '
        '{"persona_id": 1, "confidence": 0.4}]}',
        VALID,
    )
    assert r.persona_id == 2
    assert r.confidence == 0.9
    assert r.ranked_ids == [2, 3, 1]


def test_ranked_nested_json_amid_prose_is_captured_whole() -> None:
    # The greedy/DOTALL extraction must grab the whole object, not stop at the
    # first inner "}".
    r = parse_route_response(
        'Here you go:\n{"ranked": [{"persona_id": 1, "confidence": 0.8}, '
        '{"persona_id": 2, "confidence": 0.6}]}\nThanks!',
        VALID,
    )
    assert r.persona_id == 1
    assert r.ranked_ids == [1, 2]


def test_ranked_low_confidence_top_still_ranks() -> None:
    # Below-threshold #1 -> persona_id None (caller uses fallback) BUT ranked_ids
    # is still populated so the union scope can widen for ambiguous questions.
    r = parse_route_response(
        '{"ranked": [{"persona_id": 1, "confidence": 0.3}, '
        '{"persona_id": 2, "confidence": 0.25}]}',
        VALID,
        min_confidence=0.5,
    )
    assert r.persona_id is None
    assert r.confidence == 0.3
    assert r.ranked_ids == [1, 2]


def test_ranked_drops_invalid_and_dedupes() -> None:
    r = parse_route_response(
        '{"ranked": [{"persona_id": 99, "confidence": 0.9}, '  # not in catalog
        '{"persona_id": 2, "confidence": 0.8}, '
        '{"persona_id": 2, "confidence": 0.7}, '  # dup
        '{"persona_id": true, "confidence": 0.6}]}',  # bool
        VALID,
    )
    assert r.persona_id == 2
    assert r.ranked_ids == [2]


def test_ranked_caps_at_max_ranked() -> None:
    r = parse_route_response(
        '{"ranked": [{"persona_id": 1, "confidence": 0.9}, '
        '{"persona_id": 2, "confidence": 0.8}, '
        '{"persona_id": 3, "confidence": 0.7}]}',
        VALID,
        max_ranked=2,
    )
    assert r.ranked_ids == [1, 2]


def test_empty_ranked_list_fails_open() -> None:
    r = parse_route_response('{"ranked": []}', VALID)
    assert r.persona_id is None
    assert r.ranked_ids == []


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


# --- fuzzy keyword matching (all words present, any order/position) -----------

_FUZZY = [_entry(1, "AC", ["task sla", "round robin", "form task", "sla expir"])]


def test_keyword_route_all_words_anywhere_non_adjacent() -> None:
    # "task sla" -> both words present, not adjacent, reversed order
    assert keyword_route("the SLA on that task was breached", _FUZZY).persona_id == 1


def test_keyword_route_requires_all_words() -> None:
    # only "task" present, "sla" missing -> no hit
    assert keyword_route("the task failed to complete", _FUZZY) is None


def test_keyword_route_prefix_matches_stem() -> None:
    # "sla expir" -> "expir" (>=4) prefixes "expired"
    assert keyword_route("our sla expired yesterday", _FUZZY).persona_id == 1


def test_keyword_route_prefix_is_start_anchored_not_substring() -> None:
    # "form task": "form" must be a word or prefix — NOT a substring of "perform"
    assert keyword_route("perform this task now", _FUZZY) is None
    assert keyword_route("fill the form for this task", _FUZZY).persona_id == 1


def test_keyword_route_hyphen_is_tokenized() -> None:
    # "round robin" keyword hits a hyphenated "round-robin" in the question
    assert keyword_route("assign it round-robin to the team", _FUZZY).persona_id == 1


# quoted keyword -> exact contiguous phrase (for abbreviations that are also
# common words, e.g. AS = Automation Suite)
_QUOTED = [_entry(1, "AS", ['"as environment"'])]


def test_keyword_route_quoted_requires_contiguous_phrase() -> None:
    # fires on the adjacent phrase...
    assert keyword_route("migrating their AS Environment to unified", _QUOTED).persona_id == 1
    # ...but NOT when "as" and "environment" are merely both present, scattered
    assert keyword_route("as a user, how do I set up the environment?", _QUOTED) is None


def test_keyword_route_quoted_and_fuzzy_coexist() -> None:
    cat = [_entry(1, "AS", ['"as environment"']), _entry(2, "AC", ["task sla"])]
    assert keyword_route("issue with the AS Environment install", cat).persona_id == 1
    assert keyword_route("the sla on this task", cat).persona_id == 2


# --- parse_intents + intent_route (semantic pre-route) ----------------------


def test_parse_intents_splits_lines_trims_and_drops_blanks() -> None:
    assert parse_intents("a task is stuck\n  assign a task  \n\n  \nsla expired") == [
        "a task is stuck",
        "assign a task",
        "sla expired",
    ]
    assert parse_intents(None) == []
    assert parse_intents("   ") == []


def test_build_catalog_parses_intents() -> None:
    cat = build_router_catalog(
        [_persona(1, description="d", intents="first intent\nsecond intent")]
    )
    assert cat[0].intents == ["first intent", "second intent"]


def test_render_intents_only_includes_entries_with_phrases() -> None:
    from danswer.secondary_llm_flows.assistant_router import _render_intents

    cat = [
        RouterCatalogEntry(persona_id=1, name="A", routing_text="x", intents=["p1", "p2"]),
        RouterCatalogEntry(persona_id=2, name="B", routing_text="x", intents=[]),
    ]
    out = _render_intents(cat)
    assert "[id=1] A" in out and "- p1" in out and "- p2" in out
    assert "B" not in out  # no intents -> excluded


class _FakeLLM:
    def __init__(self, content: str):
        self._content = content

    def invoke(self, prompt: str):
        return SimpleNamespace(content=self._content)


_INTENT_CAT = [
    RouterCatalogEntry(persona_id=1, name="A", routing_text="x", intents=["do a thing"]),
    RouterCatalogEntry(persona_id=2, name="B", routing_text="x", intents=["do b thing"]),
]


def test_intent_route_none_when_no_intents_configured() -> None:
    cat = [RouterCatalogEntry(persona_id=1, name="A", routing_text="x", intents=[])]
    assert intent_route("q", cat, _FakeLLM("{}")) is None  # LLM not even called


def test_intent_route_none_on_empty_question() -> None:
    assert intent_route("   ", _INTENT_CAT, _FakeLLM('{"persona_id": 1, "confidence": 1}')) is None


def test_intent_route_fires_on_confident_match() -> None:
    r = intent_route(
        "please do a thing",
        _INTENT_CAT,
        _FakeLLM('{"persona_id": 1, "confidence": 0.95}'),
        min_confidence=0.8,
    )
    assert r is not None and r.persona_id == 1 and r.confidence == 0.95


def test_intent_route_none_below_confidence() -> None:
    assert (
        intent_route(
            "vague question",
            _INTENT_CAT,
            _FakeLLM('{"persona_id": 1, "confidence": 0.4}'),
            min_confidence=0.8,
        )
        is None
    )


def test_intent_route_none_on_null_pick() -> None:
    assert (
        intent_route(
            "unrelated",
            _INTENT_CAT,
            _FakeLLM('{"persona_id": null, "confidence": 0}'),
        )
        is None
    )


def test_intent_route_fails_open_on_llm_error() -> None:
    class _Boom:
        def invoke(self, prompt: str):
            raise RuntimeError("model down")

    assert intent_route("do a thing", _INTENT_CAT, _Boom()) is None
