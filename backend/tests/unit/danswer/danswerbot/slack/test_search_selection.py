"""Unit tests for the /personas "Search" sentinel resolution (Search-first UX)."""
from danswer.danswerbot.slack.constants import is_search_selection
from danswer.danswerbot.slack.constants import SEARCH_PERSONA_SENTINEL


def test_is_search_selection_true_cases() -> None:
    assert is_search_selection(SEARCH_PERSONA_SENTINEL) is True
    assert is_search_selection("search") is True
    assert is_search_selection("Search") is True
    assert is_search_selection("  SEARCH  ") is True


def test_is_search_selection_false_cases() -> None:
    assert is_search_selection("123") is False  # a real persona id
    assert is_search_selection("Automation Suite") is False
    assert is_search_selection("") is False
    assert is_search_selection(None) is False
