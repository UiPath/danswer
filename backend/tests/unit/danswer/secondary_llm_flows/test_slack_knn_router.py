"""Unit tests for the kNN-over-Slack router's pure logic (vote / gate / tiebreak /
ACL intersection). The Vespa + embedding I/O (`retrieve_slack_neighbors`) is not
exercised here — `knn_route` takes already-retrieved neighbors so the decision
logic is testable without a live index."""
from danswer.secondary_llm_flows.assistant_router import RouterCatalogEntry
from danswer.secondary_llm_flows.slack_knn_router import _channel_of
from danswer.secondary_llm_flows.slack_knn_router import knn_route
from danswer.secondary_llm_flows.slack_knn_router import SlackNeighbor


class _FakeLLM:
    """Returns a fixed string; records whether it was invoked."""

    def __init__(self, out: str) -> None:
        self.out = out
        self.called = False

    def invoke(self, prompt, tools=None, tool_choice=None):
        self.called = True

        class _Msg:
            content = self.out

        return _Msg()


class _BoomLLM:
    def invoke(self, prompt, tools=None, tool_choice=None):
        raise RuntimeError("llm down")


_CATALOG = [
    RouterCatalogEntry(persona_id=1, name="Orchestrator", keywords=[]),
    RouterCatalogEntry(persona_id=2, name="IntegrationService", keywords=[]),
]


def _n(channel, score, content="q"):
    return SlackNeighbor(channel=channel, score=score, content=content)


CH_MAP = {"help-orchestrator": 1, "help-integration-service": 2, "help-ownership": 99}


# --- _channel_of ------------------------------------------------------------


def test_channel_of_parses_json_string_and_dict() -> None:
    assert _channel_of({"metadata": '{"Channel": "help-x"}'}) == "help-x"
    assert _channel_of({"metadata": {"Channel": "help-y"}}) == "help-y"
    assert _channel_of({"metadata": "not json"}) is None
    assert _channel_of({}) is None


# --- knn_route: vote --------------------------------------------------------


def test_knn_route_confident_vote_no_llm() -> None:
    llm = _FakeLLM("Orchestrator")
    neighbors = [_n("help-orchestrator", 0.9), _n("help-orchestrator", 0.8),
                 _n("help-integration-service", 0.2)]
    res = knn_route("q", neighbors, CH_MAP, _CATALOG, llm)
    assert res.persona_id == 1
    assert res.confidence > 0.6
    assert not llm.called  # high confidence -> no tiebreak
    assert res.ranked_ids[0] == 1
    assert res.ambiguous is False  # confident -> no compare


def test_knn_route_no_votes_fails_open() -> None:
    # neighbors map to no persona in the catalog -> None (caller uses default)
    res = knn_route("q", [_n("help-unknown", 0.9)], CH_MAP, _CATALOG, _FakeLLM("x"))
    assert res.persona_id is None


def test_knn_route_ignores_personas_outside_catalog() -> None:
    # help-ownership -> persona 99, which is NOT in the ACL catalog -> not counted
    neighbors = [_n("help-ownership", 0.9), _n("help-orchestrator", 0.5)]
    res = knn_route("q", neighbors, CH_MAP, _CATALOG, _FakeLLM("x"))
    assert res.persona_id == 1  # only the in-catalog vote survives


# --- knn_route: LLM tiebreak on low confidence ------------------------------


def test_knn_route_low_conf_triggers_llm_tiebreak() -> None:
    # near-even split -> confidence < 0.6 -> LLM picks
    neighbors = [_n("help-orchestrator", 0.51), _n("help-integration-service", 0.49)]
    llm = _FakeLLM("IntegrationService")
    res = knn_route("q", neighbors, CH_MAP, _CATALOG, llm)
    assert llm.called
    assert res.persona_id == 2  # LLM's pick overrides the vote winner
    assert res.ranked_ids[0] == 2  # override reflected as #1 recommendation
    assert res.ambiguous is True  # close call -> compare offered


def test_knn_route_tiebreak_falls_back_to_vote_on_bad_llm_output() -> None:
    neighbors = [_n("help-orchestrator", 0.51), _n("help-integration-service", 0.49)]
    llm = _FakeLLM("some unrelated text")  # matches no candidate name
    res = knn_route("q", neighbors, CH_MAP, _CATALOG, llm)
    assert res.persona_id == 1  # falls back to the vote winner


def test_knn_route_tiebreak_fails_open_on_llm_error() -> None:
    neighbors = [_n("help-orchestrator", 0.51), _n("help-integration-service", 0.49)]
    res = knn_route("q", neighbors, CH_MAP, _CATALOG, _BoomLLM())
    assert res.persona_id == 1  # vote winner survives an LLM exception


def test_knn_route_single_candidate_skips_llm_even_if_low_conf() -> None:
    # only one distinct persona voted -> nothing to break, no LLM call
    llm = _FakeLLM("IntegrationService")
    res = knn_route("q", [_n("help-orchestrator", 0.05)], CH_MAP, _CATALOG, llm)
    assert res.persona_id == 1
    assert not llm.called
