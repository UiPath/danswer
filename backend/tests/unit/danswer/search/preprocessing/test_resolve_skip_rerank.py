"""Unit tests for _resolve_skip_rerank — the single resolver that decides
whether a query skips cross-encoder reranking.

The rule: rerank runs only when the global master switch (RERANK_ENABLED, i.e.
a GPU-backed model server is deployed) AND the per-assistant opt-in
(Persona.rerank_enabled) are both on. An explicit skip_rerank is honored as-is.
A legacy ENABLE_RERANKING_REAL_TIME_FLOW=true forces rerank as a fallback.

The function reads RERANK_ENABLED / ENABLE_RERANKING_REAL_TIME_FLOW as module
globals at call time, so we monkeypatch them on the module.
"""
from types import SimpleNamespace

import pytest

from danswer.search.preprocessing import preprocessing as pp


def _persona(rerank_enabled: bool) -> SimpleNamespace:
    # Stands in for a Persona; _resolve_skip_rerank only reads .rerank_enabled.
    return SimpleNamespace(rerank_enabled=rerank_enabled)


@pytest.fixture(autouse=True)
def _reset_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default both global flags off so each test sets only what it needs.
    monkeypatch.setattr(pp, "RERANK_ENABLED", False)
    monkeypatch.setattr(pp, "ENABLE_RERANKING_REAL_TIME_FLOW", False)


# --- explicit skip_rerank is always honored, regardless of globals/persona ---


def test_explicit_true_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "RERANK_ENABLED", True)
    assert pp._resolve_skip_rerank(True, _persona(True)) is True


def test_explicit_false_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even with everything off, an explicit "don't skip" wins.
    assert pp._resolve_skip_rerank(False, _persona(False)) is False


# --- the global x per-assistant matrix (explicit None) ---


def test_global_off_persona_on_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    # Local / GPU-free default: global off => never rerank, even if opted in.
    assert pp._resolve_skip_rerank(None, _persona(True)) is True


def test_global_on_persona_off_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "RERANK_ENABLED", True)
    assert pp._resolve_skip_rerank(None, _persona(False)) is True


def test_global_on_no_persona_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "RERANK_ENABLED", True)
    assert pp._resolve_skip_rerank(None, None) is True


def test_global_on_persona_on_reranks(monkeypatch: pytest.MonkeyPatch) -> None:
    # The one combination that actually reranks.
    monkeypatch.setattr(pp, "RERANK_ENABLED", True)
    assert pp._resolve_skip_rerank(None, _persona(True)) is False


# --- legacy fallback flag ---


def test_legacy_realtime_flag_forces_rerank(monkeypatch: pytest.MonkeyPatch) -> None:
    # Back-compat: the old env flag reranks even without the per-assistant opt-in.
    monkeypatch.setattr(pp, "ENABLE_RERANKING_REAL_TIME_FLOW", True)
    assert pp._resolve_skip_rerank(None, _persona(False)) is False
    assert pp._resolve_skip_rerank(None, None) is False
