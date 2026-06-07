"""Unit tests for _resolve_skip_llm_chunk_filter — decides whether to skip the
LLM relevance filter.

Rule: the filter runs only when the global master switch
LLM_RELEVANCE_FILTER_ENABLED AND the per-assistant opt-in
(Persona.llm_relevance_filter) are both on. The global DISABLE_LLM_CHUNK_FILTER
kill-switch always wins. An explicit skip (from the chat flow) is honored unless
the kill-switch is set. Independent of reranking (LLM-only, no GPU).
"""
from types import SimpleNamespace

import pytest

from danswer.search.preprocessing import preprocessing as pp


def _persona(llm_relevance_filter: bool) -> SimpleNamespace:
    return SimpleNamespace(llm_relevance_filter=llm_relevance_filter)


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "LLM_RELEVANCE_FILTER_ENABLED", False)


def _resolve(explicit, persona, disable=False):  # type: ignore[no-untyped-def]
    return pp._resolve_skip_llm_chunk_filter(explicit, persona, disable)


# --- kill-switch wins ---
def test_kill_switch_forces_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "LLM_RELEVANCE_FILTER_ENABLED", True)
    assert _resolve(False, _persona(True), disable=True) is True


# --- explicit override (chat) honored when not killed ---
def test_explicit_false_runs_filter() -> None:
    assert _resolve(False, _persona(False)) is False


def test_explicit_true_skips() -> None:
    assert _resolve(True, _persona(True)) is True


# --- global x per-assistant matrix (explicit None) ---
def test_global_off_persona_on_skips() -> None:
    assert _resolve(None, _persona(True)) is True


def test_global_on_persona_off_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "LLM_RELEVANCE_FILTER_ENABLED", True)
    assert _resolve(None, _persona(False)) is True


def test_global_on_no_persona_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "LLM_RELEVANCE_FILTER_ENABLED", True)
    assert _resolve(None, None) is True


def test_global_on_persona_on_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "LLM_RELEVANCE_FILTER_ENABLED", True)
    assert _resolve(None, _persona(True)) is False


# --- independence from reranking: relevance filter on with rerank irrelevant ---
def test_independent_of_rerank(monkeypatch: pytest.MonkeyPatch) -> None:
    # Relevance filter enabled while reranking is globally OFF (no GPU path).
    monkeypatch.setattr(pp, "LLM_RELEVANCE_FILTER_ENABLED", True)
    monkeypatch.setattr(pp, "RERANK_ENABLED", False)
    assert _resolve(None, _persona(True)) is False  # filter still runs
