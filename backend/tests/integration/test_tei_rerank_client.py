"""Integration test for the TEI rerank transport in CrossEncoderEnsembleModel.

When RERANK_SERVER_URL is configured, the client talks to a Hugging Face TEI
server's /rerank endpoint. TEI returns [{index, score}, ...] sorted by score,
so the client must scatter the scores back into the INPUT passage order and
wrap them as list[list[float]] (the shape semantic_reranking expects).
"""

import danswer.search.search_nlp_models as nlp


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return self._payload


def test_tei_path_scatters_scores_back_to_passage_order(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict = {}

    # TEI replies sorted by score (best first), referencing the input by index.
    def fake_post(url: str, json: dict | None = None, **_: object) -> _FakeResponse:
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(
            [
                {"index": 2, "score": 9.0},
                {"index": 0, "score": 1.0},
                {"index": 1, "score": -3.0},
            ]
        )

    monkeypatch.setattr(nlp.requests, "post", fake_post)

    model = nlp.CrossEncoderEnsembleModel(rerank_server_url="http://tei-rerank:80")
    out = model.predict("the query", ["a", "b", "c"])

    # one ensemble entry, scores back in passage order [a, b, c]
    assert out == [[1.0, -3.0, 9.0]]
    assert captured["url"] == "http://tei-rerank:80/rerank"
    assert captured["json"]["texts"] == ["a", "b", "c"]
    assert captured["json"]["raw_scores"] is True


def test_legacy_path_used_when_no_tei_url(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict = {}

    def fake_post(url: str, json: dict | None = None, **_: object) -> _FakeResponse:
        captured["url"] = url
        return _FakeResponse({"scores": [[0.1, 0.2]]})

    monkeypatch.setattr(nlp.requests, "post", fake_post)

    model = nlp.CrossEncoderEnsembleModel(rerank_server_url="")
    out = model.predict("q", ["a", "b"])

    assert out == [[0.1, 0.2]]
    # legacy model-server endpoint, NOT /rerank
    assert captured["url"].endswith("/encoder/cross-encoder-scores")
