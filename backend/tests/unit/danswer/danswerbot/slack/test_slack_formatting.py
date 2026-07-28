"""Unit tests for the upstream-ported mistune Slack renderer (formatting.py) and the
render_answer_markdown switch that A/Bs it against the in-house regex renderer."""
import re

import danswer.danswerbot.slack.blocks as blocks
from danswer.danswerbot.slack.formatting import format_slack_message
from danswer.danswerbot.slack.utils import remove_slack_entities_only


def _mistune(md: str) -> str:
    # mirror blocks.render_answer_markdown's mistune path (minus decode_escapes):
    # sanitize entities, collapse blank-line runs, strip
    out = remove_slack_entities_only(format_slack_message(md))
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def test_mistune_header_and_bold() -> None:
    assert _mistune("### Cloud\nText is **bold**.") == "*Cloud*\nText is *bold*."


def test_mistune_distinguishes_italic_from_bold() -> None:
    # the key correctness win over the regex renderer: single-* is italic (_x_),
    # double-* is bold (*x*)
    assert _mistune("*it* and **bold**") == "_it_ and *bold*"


def test_mistune_markdown_link() -> None:
    assert _mistune("see [docs](https://x.com/y)") == "see <https://x.com/y|docs>"


def test_mistune_converts_citation_marker_to_link() -> None:
    # mistune parses [[n]](url) as a link directly, so it emits a Slack link without
    # needing _process_citations_for_slack
    assert _mistune("HTTPS [[1]](https://x.com/y).") == "HTTPS <https://x.com/y|[1]>."


def test_mistune_sections_get_blank_line() -> None:
    # the spacing fix: sections must be separated by a blank line, not clubbed
    md = "### Cloud\nManaged.\n\n### Suite\n- on-prem"
    assert _mistune(md) == "*Cloud*\nManaged.\n\n*Suite*\n• on-prem"


def test_mistune_ordered_and_unordered_lists() -> None:
    assert _mistune("- a\n- b") == "• a\n• b"
    assert _mistune("1. a\n2. b") == "1. a\n2. b"


def test_render_switch_regex(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(blocks, "DANSWER_BOT_SLACK_MARKDOWN_RENDERER", "regex")
    out = blocks.render_answer_markdown("### H\nis **b** and *i*")
    # regex renderer conflates single-* italic into bold
    assert "*H*" in out and "*b*" in out and "*i*" in out


def test_render_switch_mistune(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(blocks, "DANSWER_BOT_SLACK_MARKDOWN_RENDERER", "mistune")
    out = blocks.render_answer_markdown("is **b** and *i* [x](https://y.com)")
    assert "_i_" in out and "*b*" in out and "<https://y.com|x>" in out
