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
    assert _mistune(md) == "*Cloud*\nManaged.\n\n*Suite*\n\n• on-prem"


def test_mistune_ordered_and_unordered_lists() -> None:
    assert _mistune("- a\n- b") == "• a\n• b"
    assert _mistune("1. a\n2. b") == "1. a\n2. b"


def test_mistune_nested_list_not_clubbed() -> None:
    # regression: the upstream "li: " hack glued a parent item onto its first child
    assert _mistune("- top\n    - child\n- top2") == "• top\n    • child\n• top2"


def test_mistune_table_renders_as_code_block_not_html() -> None:
    out = _mistune("| A | B |\n|---|---|\n| 1 | 2 |")
    assert out == "```\nA | B\n1 | 2\n```"
    assert "<table>" not in out


def test_mistune_blockquote_uses_native_slack_quote() -> None:
    assert _mistune("> quoted") == "> quoted"
    assert "<blockquote>" not in _mistune("> quoted")


def test_mistune_hrule_not_raw_html() -> None:
    out = _mistune("a\n\n---\n\nb")
    assert "───────" in out and "<hr>" not in out


def test_mistune_hard_linebreak_is_newline_not_br() -> None:
    # two trailing spaces = markdown hard break; ~18% of prod answers hit this and
    # must NOT leak a literal <br> into Slack
    assert _mistune("line one  \nline two") == "line one\nline two"


def test_mistune_demotes_raw_br_to_newline() -> None:
    out = _mistune("a<br>b")
    assert "<br>" not in out and out == "a\nb"


def test_mistune_escapes_stray_html_tag() -> None:
    out = _mistune("use <div> carefully")
    assert "<div>" not in out and "&lt;div&gt;" in out


def test_render_switch_regex(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(blocks, "DANSWER_BOT_SLACK_MARKDOWN_RENDERER", "regex")
    out = blocks.render_answer_markdown("### H\nis **b** and *i*")
    # regex renderer conflates single-* italic into bold
    assert "*H*" in out and "*b*" in out and "*i*" in out


def test_render_switch_mistune(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(blocks, "DANSWER_BOT_SLACK_MARKDOWN_RENDERER", "mistune")
    out = blocks.render_answer_markdown("is **b** and *i* [x](https://y.com)")
    assert "_i_" in out and "*b*" in out and "<https://y.com|x>" in out
