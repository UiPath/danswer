"""Regression tests for clean_spot_name.

The cleaned value is used as the connector / cc-pair display name when syncing
Highspot Spots; the original Spot title is what's stored in `spot_names` (used to
match the real Spot), so cleaning must only tidy whitespace/control chars and
never alter meaningful characters.
"""
from danswer.connectors.highspot.sync import clean_spot_name


def test_collapses_internal_whitespace() -> None:
    assert (
        clean_spot_name("Automation for Good  - Sustainability at UiPath")
        == "Automation for Good - Sustainability at UiPath"
    )


def test_trims_leading_and_trailing_whitespace() -> None:
    assert (
        clean_spot_name("  Healthcare & Life Sciences (HLS) Spot  ")
        == "Healthcare & Life Sciences (HLS) Spot"
    )


def test_preserves_meaningful_punctuation() -> None:
    title = "Track Your Impact: See How You're Progressing"
    assert clean_spot_name(title) == title


def test_strips_zero_width_chars_keeping_real_spaces() -> None:
    # zero-width space (U+200B) removed; the normal space is preserved.
    assert clean_spot_name("Gen​AI GTM") == "GenAI GTM"


def test_strips_control_chars() -> None:
    # control chars (e.g. tab) are removed entirely, not turned into spaces.
    assert clean_spot_name("Sales\tAMER") == "SalesAMER"


def test_already_clean_title_unchanged() -> None:
    assert clean_spot_name("Sales AMER") == "Sales AMER"
