"""Unit tests for markdown_to_slack — converting LLM markdown (esp. the multi-section
Deep RAG synthesis) into Slack mrkdwn."""
from danswer.danswerbot.slack.utils import markdown_to_slack


def test_headers_become_bold() -> None:
    assert markdown_to_slack("### Automation Cloud") == "*Automation Cloud*"
    # a blank line is forced before each section title so sections don't club together
    assert markdown_to_slack("## H\n# H2") == "*H*\n\n*H2*"


def test_section_gets_blank_line_before_it() -> None:
    # single-newline synthesis output must still render with separation in Slack
    assert (
        markdown_to_slack("body text\n### Next Section\nmore")
        == "body text\n\n*Next Section*\nmore"
    )


def test_double_star_bold_to_single() -> None:
    assert markdown_to_slack("this is **bold** here") == "this is *bold* here"


def test_markdown_link_to_slack_link() -> None:
    assert (
        markdown_to_slack("see [the docs](https://x.com/y)")
        == "see <https://x.com/y|the docs>"
    )


def test_bullets_to_dot() -> None:
    assert markdown_to_slack("- one\n- two") == "• one\n• two"


def test_header_with_inline_bold_no_triple_star() -> None:
    # ** collapses to * first, then the header wrap must not double it up
    assert markdown_to_slack("### **Cloud**") == "*Cloud*"


def test_plain_text_unchanged() -> None:
    assert markdown_to_slack("just a normal sentence.") == "just a normal sentence."


def test_markdown_link_survives_after_sanitizer() -> None:
    # blocks.py runs remove_slack_text_interactions BEFORE markdown_to_slack. The
    # sanitizer's replace_links() collapses <url|text> -> text, so a raw markdown link
    # must only become a Slack link at the markdown_to_slack step (and survive intact).
    from danswer.danswerbot.slack.utils import remove_slack_text_interactions

    raw = "see [docs](https://x.com/y)"
    assert markdown_to_slack(remove_slack_text_interactions(raw)) == (
        "see <https://x.com/y|docs>"
    )


def test_citation_marker_untouched() -> None:
    # [[n]](url) citation markers must pass through unchanged so the downstream
    # _process_citations_for_slack can convert them.
    text = "answer [[1]](https://x.com/y)."
    assert markdown_to_slack(text) == text


def test_multi_section_answer() -> None:
    md = "### Cloud\nServerless robots.\n\n### Suite\n- on-prem\n- self-managed"
    out = markdown_to_slack(md)
    assert "*Cloud*" in out and "*Suite*" in out
    assert "• on-prem" in out and "• self-managed" in out
    assert "###" not in out
