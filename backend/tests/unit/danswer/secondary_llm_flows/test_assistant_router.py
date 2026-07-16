"""Unit tests for the assistant router's pure logic.

The router pipeline is: `keyword_route` (deterministic, here) -> kNN-over-Slack
fallback (see test_slack_knn_router). `build_router_catalog` includes EVERY
ACL-filtered persona (its id fences the kNN router) and parses each one's
comma-separated `routing_keywords`.
"""
from types import SimpleNamespace

from danswer.secondary_llm_flows.assistant_router import build_router_catalog
from danswer.secondary_llm_flows.assistant_router import keyword_route
from danswer.secondary_llm_flows.assistant_router import RouterCatalogEntry


def _persona(persona_id: int, keywords: str | None = None):
    return SimpleNamespace(
        id=persona_id,
        name=f"persona-{persona_id}",
        routing_keywords=keywords,
    )


# --- build_router_catalog ---------------------------------------------------


def test_catalog_includes_every_persona_even_without_keywords() -> None:
    # The kNN router routes by id; an assistant needs no keywords to be routable,
    # so NONE are skipped (unlike the old routing_instructions-gated catalog).
    catalog = build_router_catalog([_persona(1), _persona(2, keywords="foo")])
    assert [e.persona_id for e in catalog] == [1, 2]
    assert catalog[0].keywords == []


def test_build_catalog_parses_keywords() -> None:
    cat = build_router_catalog([_persona(1, keywords="Foo, Bar Baz , ")])
    assert cat[0].keywords == ["foo", "bar baz"]  # trimmed + lowercased, blanks dropped


# --- keyword_route (deterministic pre-route) --------------------------------


def _entry(pid, name, keywords):
    return RouterCatalogEntry(persona_id=pid, name=name, keywords=keywords)


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
    # falls through to the kNN router (purely additive)
    assert keyword_route("what is the weather today", _KW_CATALOG) is None
    assert keyword_route("", _KW_CATALOG) is None


def test_keyword_route_longest_match_wins() -> None:
    cat = [
        _entry(1, "AS", ["as"]),
        _entry(2, "AutomationSuite", ["automation suite"]),
    ]
    # "automation suite" (longer) beats the substring "as"
    assert keyword_route("automation suite sizing", cat).persona_id == 2


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


# quality-aware ranking: contiguous/exact match beats scattered/prefix match
_AS_IS = [
    _entry(1, "AutomationSuite", ["automation suite"]),
    _entry(2, "IntegrationService", ["integration service"]),
]
# both fuzzy-match this AutomationSuite question: "automation suite" is verbatim,
# "integration service" only via the incidental plurals integration(s)/service(s)
_ARCH_Q = "architecture diagram for the automation suite release with integrations and different services"


def test_keyword_route_contiguous_exact_beats_scattered_prefix() -> None:
    # AutomationSuite wins: its words are adjacent + exact; IntegrationService's
    # matched only scattered via prefix. (Char length would have picked IS before.)
    assert keyword_route(_ARCH_Q, _AS_IS).persona_id == 1


def test_keyword_route_priority_always_wins() -> None:
    # Tag IntegrationService's keyword as always-wins ("!") -> it beats the
    # stronger AutomationSuite match.
    cat = [
        _entry(1, "AutomationSuite", ["automation suite"]),
        _entry(2, "IntegrationService", ["!integration service"]),
    ]
    assert keyword_route(_ARCH_Q, cat).persona_id == 2


def test_keyword_route_priority_beats_across_the_board() -> None:
    # A priority keyword outranks a non-priority one even if the latter is more
    # specific (more words / longer).
    cat = [
        _entry(1, "A", ["!coupa"]),
        _entry(2, "B", ["procure to pay solution"]),
    ]
    assert keyword_route("coupa invoice in the procure to pay solution", cat).persona_id == 1


def test_keyword_route_priority_exact_combo() -> None:
    cat = [_entry(1, "AC", ['!"validation station"'])]
    assert keyword_route("stuck in the validation station today", cat).persona_id == 1
    # quoted still requires the contiguous phrase even with priority
    assert keyword_route("validation of the station data", cat) is None
