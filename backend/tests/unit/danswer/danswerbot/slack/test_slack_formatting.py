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


def test_mistune_header_with_inline_bold_not_double_starred() -> None:
    # regression: "### 1. **X**" must become "*1. X*" (numbered + bold), NOT the broken
    # "*1. *X**" where the wrapping star collides with the inline bold's star
    assert (
        _mistune("### 1. **Deleting Docker Images**") == "*1. Deleting Docker Images*"
    )
    assert _mistune("## **Removing Tags**") == "*Removing Tags*"
    assert _mistune("### Deleting **old** images") == "*Deleting old images*"


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
    # bullets stay tight; numbered points get a blank line between them
    assert _mistune("- a\n- b") == "• a\n• b"
    assert _mistune("1. a\n2. b") == "1. a\n\n2. b"


def test_mistune_loose_list_no_blank_between_point_and_subbullets() -> None:
    # a "loose" list (blank lines in source) paragraph-wraps the lead line; the number
    # must stay directly above its own sub-bullets, with the blank line only BETWEEN
    # numbered points
    md = "1. **H:**\n\n    - a\n    - b\n\n2. **H2:**\n\n    - c"
    assert _mistune(md) == "1. *H:*\n    • a\n    • b\n\n2. *H2:*\n    • c"


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


def test_mistune_hrule_dropped_to_single_newline() -> None:
    # a horizontal rule collapses to a single newline between blocks — no visible
    # line, no blank line, no raw <hr>
    out = _mistune("a\n\n---\n\nb")
    assert out == "a\nb"
    assert "<hr>" not in out and "───" not in out


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


def test_split_text_never_orphans_bold_across_blocks() -> None:
    # a heading straddling the 3000-char SectionBlock boundary must not be cut mid-line
    # (that orphans a '*' and breaks the bolding, e.g. "Dependencies:*")
    heading = "*Best Practices for Handling Dependencies:*"
    for pad in range(2950, 3050):
        text = ("word " * 900)[:pad] + "\n" + heading + "\nmore body text here."
        chunks = blocks._split_text(text)
        lines = [ln for c in chunks for ln in c.splitlines()]
        assert any(ln.strip() == heading for ln in lines), f"heading split at pad={pad}"
        for c in chunks:
            assert c.count("*") % 2 == 0, f"orphaned '*' at pad={pad}"
        assert all(len(c) <= 3000 for c in chunks)


def test_answer_footer_sections_bold_and_spaced(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The AutomationSuite persona's "[SME]:" / "[Confidence Score]:" lines + the
    # authoritative-sources footer must each render as their own BOLD, blank-line-
    # separated section (in BOTH renderers) — not plain/cramped onto the answer's end.
    answer = (
        "Answer:\n"
        "- point one [1].\n"
        "[SME]: Quentin Brandon.\n"
        "[Confidence Score]: 100.\n\n"
        "**Authoritative sources:**\n"
        "- [Doc A](https://a.com)"
    )
    for renderer in ("mistune", "regex"):
        monkeypatch.setattr(blocks, "DANSWER_BOT_SLACK_MARKDOWN_RENDERER", renderer)
        out = blocks.render_answer_markdown(answer)
        assert "*Answer:*" in out, f"Answer header not bold ({renderer})"
        for section in ("*SME:*", "*Confidence Score:*", "*Authoritative sources:*"):
            assert section in out, f"{section} not bold ({renderer})"
            assert (
                f"\n\n{section}" in out
            ), f"{section} not blank-line-separated ({renderer})"
