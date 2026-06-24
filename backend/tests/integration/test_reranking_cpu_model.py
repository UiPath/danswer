"""Integration test: cross-encoder reranking with a REAL model on CPU.

Loads a small cross-encoder (ms-marco-MiniLM-L-6-v2 — tiny, CPU-friendly) and
runs it through the real rerank_chunks / semantic_reranking ordering logic to
prove a relevant chunk gets reordered to the top even when retrieval put an
irrelevant one first. Uses the small MiniLM model (not the prod
bge-reranker-v2-m3) because the *ordering logic* is what's under test, and it
keeps the download light. Skips cleanly if the model can't be fetched (offline).
"""
from types import SimpleNamespace

import pytest

pytest.importorskip("sentence_transformers")


def _load_cross_encoder():  # type: ignore[no-untyped-def]
    from sentence_transformers import CrossEncoder

    try:
        return CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
    except Exception as exc:  # network / offline / disk
        pytest.skip(f"cross-encoder model unavailable: {exc}")


def _chunk(content: str, score: float) -> SimpleNamespace:
    # SimpleNamespace stands in for InferenceChunk — semantic_reranking only
    # does attribute access (content, boost, recency_bias, score).
    return SimpleNamespace(
        content=content,
        boost=0,
        recency_bias=1.0,
        score=score,
        document_id="d",
        chunk_id=0,
        source_links=None,
    )


def test_reranker_reorders_relevant_chunk_to_top(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    model = _load_cross_encoder()

    import danswer.search.postprocessing.postprocessing as pp

    class _RealEnsemble:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def predict(self, query: str, passages: list[str]) -> list[list[float]]:
            scores = model.predict([(query, p) for p in passages])
            return [[float(s) for s in scores]]

    monkeypatch.setattr(pp, "CrossEncoderEnsembleModel", _RealEnsemble)

    # Deliberately WRONG initial order: the off-topic chunk has the top score.
    chunks = [
        _chunk("Bananas are a good source of potassium.", score=0.9),
        _chunk("The capital of France is Paris.", score=0.5),
        _chunk("Python is a programming language.", score=0.4),
    ]
    query = SimpleNamespace(query="What is the capital of France?", num_rerank=15)

    ranked = pp.rerank_chunks(query=query, chunks_to_rerank=chunks)

    # After reranking, the France/Paris chunk should be on top, not the banana.
    assert "Paris" in ranked[0].content
    assert ranked[0].content != "Bananas are a good source of potassium."
