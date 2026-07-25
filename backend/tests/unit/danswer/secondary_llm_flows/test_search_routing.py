"""Unit tests for resolve_search_persona — the shared assistant-routing decision
used by BOTH the web auto-search and the Slack bot's Search mode. The two router
primitives (keyword_route, knn_route) are stubbed; we assert the orchestration:
keyword override wins first, kNN is the fallback, and everything fails OPEN to the
all-source default."""
from danswer.secondary_llm_flows import search_routing as sr
from danswer.secondary_llm_flows.assistant_router import RouteResult

_DB = object()
_LLM = object()
_CATALOG: list = []


def _no_keyword(monkeypatch):
    monkeypatch.setattr(sr, "keyword_route", lambda q, c: None)


def _stub_knn(monkeypatch, result):
    monkeypatch.setattr(sr, "retrieve_slack_neighbors", lambda q, db: [])
    monkeypatch.setattr(sr, "build_channel_persona_map", lambda db: {})
    monkeypatch.setattr(sr, "knn_route", lambda *a, **k: result)


def test_keyword_route_wins_and_skips_knn(monkeypatch) -> None:
    monkeypatch.setattr(
        sr, "keyword_route", lambda q, c: RouteResult(persona_id=7, confidence=1.0)
    )
    knn_calls: list = []
    monkeypatch.setattr(sr, "knn_route", lambda *a, **k: knn_calls.append(1))
    monkeypatch.setattr(sr, "retrieve_slack_neighbors", lambda q, db: knn_calls.append(1))

    res = sr.resolve_search_persona("reset orchestrator robot", _CATALOG, _LLM, _DB)

    assert res.persona_id == 7
    assert res.routed is True
    assert res.confidence == 1.0
    assert res.ranked_ids == []  # keyword routes carry no recommendations
    assert knn_calls == []  # keyword hit -> kNN path never entered


def test_knn_fallback_routes_and_ranks(monkeypatch) -> None:
    _no_keyword(monkeypatch)
    _stub_knn(
        monkeypatch,
        RouteResult(persona_id=12, confidence=0.8, ranked_ids=[12, 5, 9], ambiguous=True),
    )

    res = sr.resolve_search_persona("how do I configure X", _CATALOG, _LLM, _DB)

    assert res.persona_id == 12
    assert res.routed is True
    assert res.confidence == 0.8
    assert res.ranked_ids == [12, 5, 9]  # [1:] are the recommended assistants
    assert res.ambiguous is True


def test_knn_no_match_falls_open_to_default(monkeypatch) -> None:
    _no_keyword(monkeypatch)
    _stub_knn(monkeypatch, RouteResult(persona_id=None, confidence=0.0, ranked_ids=[]))

    res = sr.resolve_search_persona("vague question", _CATALOG, _LLM, _DB)

    assert res.persona_id == sr.DEFAULT_SEARCH_PERSONA_ID
    assert res.routed is False


def test_knn_exception_fails_open(monkeypatch) -> None:
    _no_keyword(monkeypatch)

    def _boom(*a, **k):  # type: ignore
        raise RuntimeError("vespa down")

    monkeypatch.setattr(sr, "retrieve_slack_neighbors", _boom)

    res = sr.resolve_search_persona("anything", _CATALOG, _LLM, _DB)

    assert res.persona_id == sr.DEFAULT_SEARCH_PERSONA_ID
    assert res.routed is False
    assert res.ranked_ids == []


# --- global admin rulebook stage (between keyword and kNN) -------------------
from danswer.secondary_llm_flows.assistant_router import RouterCatalogEntry  # noqa: E402

_CAT = [
    RouterCatalogEntry(persona_id=122, name="Fantastic", keywords=[]),
    RouterCatalogEntry(persona_id=315, name="Ownership", keywords=[]),
]


class _FakeLLM:
    def __init__(self, out):
        self._out = out
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return self._out


def test_global_rule_route_matches_named_assistant(monkeypatch) -> None:
    monkeypatch.setattr(sr, "message_to_string", lambda x: x)
    r = sr.global_rule_route("who owns pepsico", _CAT, _FakeLLM("Ownership"), "owner Qs -> Ownership")
    assert r is not None and r.persona_id == 315 and r.confidence == 1.0


def test_global_rule_route_none_response(monkeypatch) -> None:
    monkeypatch.setattr(sr, "message_to_string", lambda x: x)
    assert sr.global_rule_route("hi", _CAT, _FakeLLM("NONE"), "rules") is None


def test_global_rule_route_hallucinated_name_rejected(monkeypatch) -> None:
    monkeypatch.setattr(sr, "message_to_string", lambda x: x)
    assert sr.global_rule_route("q", _CAT, _FakeLLM("NotInCatalog"), "rules") is None


def test_global_rule_route_empty_rules_skips_llm() -> None:
    llm = _FakeLLM("Ownership")
    assert sr.global_rule_route("q", _CAT, llm, "") is None
    assert sr.global_rule_route("q", _CAT, llm, "   ") is None
    assert llm.calls == 0  # no LLM call when the rulebook is empty


def test_global_rule_route_llm_exception_fails_open(monkeypatch) -> None:
    monkeypatch.setattr(sr, "message_to_string", lambda x: x)

    class _Boom:
        def invoke(self, p):
            raise RuntimeError("boom")

    assert sr.global_rule_route("q", _CAT, _Boom(), "rules") is None


def test_keyword_beats_rule(monkeypatch) -> None:
    monkeypatch.setattr(sr, "keyword_route", lambda q, c: RouteResult(persona_id=7, confidence=1.0))
    rule_calls: list = []
    monkeypatch.setattr(sr, "global_rule_route", lambda *a, **k: rule_calls.append(1))
    res = sr.resolve_search_persona("q", _CAT, object(), object(), rules_prompt="rules")
    assert res.persona_id == 7 and rule_calls == []  # keyword wins; rule never consulted


def test_rule_runs_between_keyword_and_knn(monkeypatch) -> None:
    monkeypatch.setattr(sr, "keyword_route", lambda q, c: None)
    monkeypatch.setattr(sr, "global_rule_route", lambda q, c, llm, rules: RouteResult(persona_id=315, confidence=1.0))
    knn_calls: list = []
    monkeypatch.setattr(sr, "retrieve_slack_neighbors", lambda q, db: knn_calls.append(1))
    res = sr.resolve_search_persona("who owns X", _CAT, object(), object(), rules_prompt="owner -> Ownership")
    assert res.persona_id == 315 and res.routed is True and knn_calls == []  # rule overrides kNN


def test_rule_skipped_when_flag_off_no_prompt(monkeypatch) -> None:
    monkeypatch.setattr(sr, "keyword_route", lambda q, c: None)
    rule_calls: list = []
    monkeypatch.setattr(sr, "global_rule_route", lambda *a, **k: rule_calls.append(1))
    monkeypatch.setattr(sr, "retrieve_slack_neighbors", lambda q, db: [])
    monkeypatch.setattr(sr, "build_channel_persona_map", lambda db: {})
    monkeypatch.setattr(sr, "knn_route", lambda *a, **k: RouteResult(persona_id=5, confidence=0.7, ranked_ids=[5]))
    res = sr.resolve_search_persona("q", _CAT, object(), object(), rules_prompt=None)
    assert res.persona_id == 5 and rule_calls == []  # no prompt -> rule stage skipped -> kNN


def test_rule_miss_falls_through_to_knn(monkeypatch) -> None:
    monkeypatch.setattr(sr, "keyword_route", lambda q, c: None)
    monkeypatch.setattr(sr, "global_rule_route", lambda *a, **k: None)  # rule didn't match
    monkeypatch.setattr(sr, "retrieve_slack_neighbors", lambda q, db: [])
    monkeypatch.setattr(sr, "build_channel_persona_map", lambda db: {})
    monkeypatch.setattr(sr, "knn_route", lambda *a, **k: RouteResult(persona_id=5, confidence=0.7, ranked_ids=[5]))
    res = sr.resolve_search_persona("q", _CAT, object(), object(), rules_prompt="rules")
    assert res.persona_id == 5
