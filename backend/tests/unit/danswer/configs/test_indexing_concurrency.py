"""Unit tests for the per-source indexing concurrency cap helpers."""
import pytest

from danswer.configs.indexing_concurrency import _resolve_overrides
from danswer.configs.indexing_concurrency import cap_for_source


def test_cap_for_source_uses_override_when_present() -> None:
    assert cap_for_source("web", default=1, overrides={"web": 3}) == 3


def test_cap_for_source_falls_back_to_default() -> None:
    assert cap_for_source("slack", default=1, overrides={"web": 3}) == 1


def test_cap_for_source_zero_override_is_respected() -> None:
    # 0 = uncapped; must not be treated as falsy/absent.
    assert cap_for_source("web", default=1, overrides={"web": 0}) == 0


def test_resolve_overrides_parses_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INDEXING_PER_SOURCE_CAP_OVERRIDES", "web=0,slack=1")
    assert _resolve_overrides() == {"web": 0, "slack": 1}


def test_resolve_overrides_lowercases_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INDEXING_PER_SOURCE_CAP_OVERRIDES", "WEB=3")
    assert _resolve_overrides() == {"web": 3}


def test_resolve_overrides_skips_malformed_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "web" (no =), "=5" (empty source), "slack=abc" (bad int) all skipped;
    # one typo must not wipe the valid entries.
    monkeypatch.setenv(
        "INDEXING_PER_SOURCE_CAP_OVERRIDES", "web, =5, slack=abc, confluence=2"
    )
    assert _resolve_overrides() == {"confluence": 2}


def test_resolve_overrides_clamps_negative_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INDEXING_PER_SOURCE_CAP_OVERRIDES", "web=-2")
    assert _resolve_overrides() == {"web": 0}


def test_resolve_overrides_empty_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INDEXING_PER_SOURCE_CAP_OVERRIDES", raising=False)
    assert _resolve_overrides() == {}
