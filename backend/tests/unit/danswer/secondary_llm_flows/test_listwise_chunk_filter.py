"""Unit tests for the listwise (one-shot) relevance filter parsing.

The filter asks the LLM, in a single call, to return a JSON array of the useful
section numbers. Parsing must: extract the array even amid prose, treat an empty
array as a valid "none useful", clamp out-of-range numbers, and FAIL OPEN (keep
all chunks) when no array can be parsed.
"""
from danswer.secondary_llm_flows.chunk_usefulness import _parse_useful_indices
from danswer.secondary_llm_flows.chunk_usefulness import llm_eval_chunks_listwise


def test_parses_array() -> None:
    assert _parse_useful_indices("[1, 3, 4]", count=5) == {1, 3, 4}


def test_parses_array_amid_prose() -> None:
    assert _parse_useful_indices("Sure! The useful ones are [2, 5].", count=5) == {2, 5}


def test_empty_array_means_none_useful() -> None:
    # Explicit [] is a valid answer (not a parse failure) → empty set.
    assert _parse_useful_indices("[]", count=5) == set()


def test_out_of_range_clamped() -> None:
    assert _parse_useful_indices("[0, 2, 9]", count=3) == {2}


def test_no_array_is_parse_failure() -> None:
    # None signals the caller to fail OPEN.
    assert _parse_useful_indices("I cannot decide.", count=3) is None


def test_empty_input_returns_empty() -> None:
    assert llm_eval_chunks_listwise("q", [], llm=None) == []  # type: ignore[arg-type]


def test_fail_open_on_llm_error() -> None:
    class _BoomLLM:
        def invoke(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("model down")

    # Any exception → keep all chunks (all True).
    out = llm_eval_chunks_listwise("q", ["a", "b", "c"], llm=_BoomLLM())  # type: ignore[arg-type]
    assert out == [True, True, True]
