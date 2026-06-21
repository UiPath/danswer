"""Unit tests for the authoritative-sources citation nudge.

Generic, global instruction (derived from PROTECTED_SOURCES) telling the LLM to
prefer grounding + citing authoritative systems of record over chat discussions.
Source names must match the `Source: X` labels build_doc_context_str puts on docs
(i.e. clean_up_source), so the model can map the nudge to specific docs.
"""
import pytest

from danswer.prompts import prompt_utils as pu


def test_lists_protected_sources_with_doc_label_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pu, "PROTECTED_SOURCES", ["web", "outsystems", "highspot"])
    out = pu.build_authoritative_sources_reminder()
    # clean_up_source: web -> "Website" (CONNECTOR_NAME_MAP), others title-cased.
    assert "Website" in out
    assert "Outsystems" in out
    assert "Highspot" in out
    # matches the doc labels and instructs preference over discussions.
    assert "authoritative" in out.lower()
    assert "chat discussions" in out.lower()


def test_names_match_clean_up_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pu, "PROTECTED_SOURCES", ["sfkbarticles", "web"])
    out = pu.build_authoritative_sources_reminder()
    # Whatever build_doc_context_str would label these, the nudge must use the same.
    assert pu.clean_up_source("sfkbarticles") in out
    assert pu.clean_up_source("web") in out


def test_empty_when_no_protected_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pu, "PROTECTED_SOURCES", [])
    assert pu.build_authoritative_sources_reminder() == ""


def test_dedupes_repeated_display_names(monkeypatch: pytest.MonkeyPatch) -> None:
    # If two keys cleaned to the same display name, list it once.
    monkeypatch.setattr(pu, "PROTECTED_SOURCES", ["web", "web"])
    out = pu.build_authoritative_sources_reminder()
    assert out.count("Website") == 1


def test_only_appended_to_task_prompt_when_citations_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pu, "PROTECTED_SOURCES", ["outsystems"])

    class _P:
        task_prompt = "BASE."
        include_citations = True

    class _PNo:
        task_prompt = "BASE."
        include_citations = False

    with_cite = pu.build_task_prompt_reminders(_P(), use_language_hint=False, citation_str="CITE.")
    no_cite = pu.build_task_prompt_reminders(_PNo(), use_language_hint=False, citation_str="CITE.")
    assert "authoritative systems of record" in with_cite
    assert "Outsystems" in with_cite
    assert "authoritative systems of record" not in no_cite
