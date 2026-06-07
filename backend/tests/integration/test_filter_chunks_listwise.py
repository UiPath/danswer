"""Integration test: the one-shot LLM relevance filter end-to-end (filter_chunks).

Drives the real filter_chunks → llm_eval_chunks_listwise → _parse_useful_indices
path with a stub LLM (we can't run a real chat model locally), verifying the
listwise selection maps to the right chunk ids and that it fails OPEN.
"""
from types import SimpleNamespace

from danswer.search.postprocessing.postprocessing import filter_chunks


class _StubLLM:
    """Minimal LLM whose .invoke returns a message with the given content
    (message_to_string only needs `.content` to be a str)."""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    def invoke(self, *_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(content=self._reply)


def _chunks(n: int) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(content=f"chunk number {i}", unique_id=f"u{i}")
        for i in range(n)
    ]


def _query() -> SimpleNamespace:
    return SimpleNamespace(query="some question", max_llm_filter_chunks=15)


def test_listwise_selection_maps_to_chunk_ids() -> None:
    # LLM says sections 1 and 3 are useful (1-based) → chunks u0 and u2.
    out = filter_chunks(_query(), _chunks(3), _StubLLM("[1, 3]"))
    assert out == ["u0", "u2"]


def test_none_useful() -> None:
    out = filter_chunks(_query(), _chunks(3), _StubLLM("[]"))
    assert out == []


def test_fail_open_on_unparseable_reply() -> None:
    # No JSON array → keep everything (fail open).
    out = filter_chunks(_query(), _chunks(3), _StubLLM("I'm not sure."))
    assert out == ["u0", "u1", "u2"]
