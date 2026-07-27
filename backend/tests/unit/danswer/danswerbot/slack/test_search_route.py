"""Unit tests for the Slack Search-mode auto-route (_route_search_persona).

Asserts the ACL contract, fail-open behavior, and recommendation mapping:
  - only routes for a Slack sender that resolves to a Danswer user
  - returns (routed_assistant, recommended_names) when the resolver picks one
  - keeps the all-source default (None) otherwise, and on any error
  - maps the router's ranked ids -> display names, excluding whoever answered.
DB session + resolver are stubbed on the module.
"""
import types

from danswer.danswerbot.slack.handlers import handle_message as hm
from danswer.secondary_llm_flows.search_routing import RouteResolution


class _FakeSession:
    def __enter__(self):
        return "db"

    def __exit__(self, *a):
        return False


class _FakeUser:
    id = 42


class _FakeClient:
    def __init__(self, email: str | None = "u@x.com", raise_: bool = False) -> None:
        self._email = email
        self._raise = raise_

    def users_info(self, user):  # type: ignore
        if self._raise:
            raise RuntimeError("slack api down")

        class _R:
            data = {"user": {"profile": {"email": self._email}}}

        return _R()


def _patch_db(monkeypatch) -> None:
    monkeypatch.setattr(hm, "get_sqlalchemy_engine", lambda: None)
    monkeypatch.setattr(hm, "Session", lambda engine: _FakeSession())
    monkeypatch.setattr(hm, "build_router_catalog_for_user", lambda u, db: [])
    monkeypatch.setattr(hm, "get_router_llm", lambda: object())


def test_unresolved_sender_never_routes(monkeypatch) -> None:
    _patch_db(monkeypatch)
    monkeypatch.setattr(hm, "get_user_by_email", lambda email, db_session: None)
    resolve_calls: list = []
    monkeypatch.setattr(
        hm, "resolve_search_persona", lambda *a, **k: resolve_calls.append(1)
    )

    persona, recs = hm._route_search_persona("q", sender_id="U1", client=_FakeClient())

    assert persona is None
    assert recs == []
    assert resolve_calls == []  # ACL: routing not even attempted for unknown sender


def test_no_sender_id_returns_none(monkeypatch) -> None:
    _patch_db(monkeypatch)
    monkeypatch.setattr(hm, "get_user_by_email", lambda email, db_session: None)
    monkeypatch.setattr(hm, "resolve_search_persona", lambda *a, **k: None)

    persona, recs = hm._route_search_persona("q", sender_id=None, client=_FakeClient())

    assert persona is None
    assert recs == []


def test_routes_to_assistant_when_resolver_picks_one(monkeypatch) -> None:
    _patch_db(monkeypatch)
    routed_persona = object()
    monkeypatch.setattr(hm, "get_user_by_email", lambda email, db_session: _FakeUser())
    monkeypatch.setattr(
        hm,
        "resolve_search_persona",
        lambda *a, **k: RouteResolution(
            persona_id=12, confidence=0.9, ranked_ids=[12], ambiguous=False, routed=True
        ),
    )
    monkeypatch.setattr(
        hm,
        "get_persona_with_docset_and_prompts",
        lambda persona_id, db_session: routed_persona,
    )

    persona, recs = hm._route_search_persona(
        "reset orchestrator", sender_id="U1", client=_FakeClient()
    )

    assert persona is routed_persona


def test_default_route_returns_none(monkeypatch) -> None:
    _patch_db(monkeypatch)
    monkeypatch.setattr(hm, "get_user_by_email", lambda email, db_session: _FakeUser())
    monkeypatch.setattr(
        hm,
        "resolve_search_persona",
        lambda *a, **k: RouteResolution(
            persona_id=0, confidence=0.0, ranked_ids=[], ambiguous=False, routed=False
        ),
    )

    persona, recs = hm._route_search_persona(
        "vague", sender_id="U1", client=_FakeClient()
    )

    assert persona is None  # not routed -> keep all-source default


def test_fails_open_on_exception(monkeypatch) -> None:
    _patch_db(monkeypatch)
    monkeypatch.setattr(hm, "get_user_by_email", lambda email, db_session: _FakeUser())

    def _boom(*a, **k):  # type: ignore
        raise RuntimeError("boom")

    monkeypatch.setattr(hm, "resolve_search_persona", _boom)

    persona, recs = hm._route_search_persona("q", sender_id="U1", client=_FakeClient())

    assert persona is None
    assert recs == []


def test_recommendations_mapped_and_exclude_answerer(monkeypatch) -> None:
    _patch_db(monkeypatch)
    monkeypatch.setattr(hm, "get_user_by_email", lambda email, db_session: _FakeUser())
    catalog = [
        types.SimpleNamespace(persona_id=12, name="Automation Suite"),
        types.SimpleNamespace(persona_id=5, name="Orchestrator"),
        types.SimpleNamespace(persona_id=9, name="Discovery"),
    ]
    monkeypatch.setattr(hm, "build_router_catalog_for_user", lambda u, db: catalog)
    monkeypatch.setattr(
        hm,
        "resolve_search_persona",
        lambda *a, **k: RouteResolution(
            persona_id=12,
            confidence=0.8,
            ranked_ids=[12, 5, 9],
            ambiguous=False,
            routed=True,
        ),
    )
    routed_persona = object()
    monkeypatch.setattr(
        hm,
        "get_persona_with_docset_and_prompts",
        lambda persona_id, db_session: routed_persona,
    )

    persona, recs = hm._route_search_persona("q", sender_id="U1", client=_FakeClient())

    assert persona is routed_persona
    # the answerer (12) is dropped; ranks 2..N map to display names
    assert recs == ["Orchestrator", "Discovery"]


def test_search_footer_block_with_recommendations() -> None:
    blocks = hm._build_search_footer_block(
        "Automation Suite", ["Orchestrator", "Discovery"]
    )
    assert len(blocks) == 2  # divider + context
    text = blocks[1].to_dict()["elements"][0]["text"]  # serializes to valid Slack shape
    assert "Automation Suite" in text
    assert "Orchestrator" in text and "Discovery" in text
    assert "/personas" in text


def test_search_footer_block_without_recommendations() -> None:
    blocks = hm._build_search_footer_block("Search", [])
    text = blocks[1].to_dict()["elements"][0]["text"]
    assert "Search" in text
    assert "Also relevant" not in text  # no recs -> attribution only
