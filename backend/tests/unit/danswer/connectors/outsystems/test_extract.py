"""Tests for outsystems page text/title extraction.

The connector fetches each page as an OutSystems screenservice JSON and pulls
content from `PageWidgetItem.Text1` (HTML) anywhere in the section/widget tree,
with the title from `Page2.Name` (falling back to the first heading). These guard
that extraction against schema drift and HTML edge cases.
"""
import time

from danswer.connectors.outsystems import connector as os_connector
from danswer.connectors.outsystems.connector import collect_widgets
from danswer.connectors.outsystems.connector import extract_page_text
from danswer.connectors.outsystems.connector import extract_page_title
from danswer.connectors.outsystems.connector import _extract_text_with_timeout


def _page(*text1_blobs: str, name: str = "") -> dict:
    items = [{"PageWidgetItem": {"Text1": b}} for b in text1_blobs]
    return {
        "Page2": {"Name": name},
        "StrPageSectionRowList": {
            "List": [
                {"StrPageSectionList": {"List": [{"WidgetItems": {"List": items}}]}}
            ]
        },
    }


def test_extracts_and_strips_html_across_widgets() -> None:
    data = _page(
        "<h2>How to report</h2><p>Email <a href='x'>ethics@uipath.com</a> &amp; call.</p>",
        "<ul><li>Step one</li><li>Step two</li></ul>",
        name="Reporting an ethics concern",
    )
    body = extract_page_text(data)
    assert "How to report" in body
    assert "ethics@uipath.com & call." in body  # entity unescaped, tags gone
    assert "Step one" in body and "Step two" in body
    assert "<" not in body and ">" not in body


def test_title_from_page2_name() -> None:
    data = _page("<p>body text here</p>", name="Reporting an ethics concern")
    assert (
        extract_page_title(data, extract_page_text(data), 314)
        == "Reporting an ethics concern"
    )


def test_title_falls_back_to_first_heading_when_name_blank() -> None:
    data = _page("<h1>Welcome to the Policy Hub</h1><p>more</p>", name="")
    assert extract_page_title(data, extract_page_text(data), 7) == (
        "Welcome to the Policy Hub"
    )


def test_title_final_fallback_uses_page_id() -> None:
    assert extract_page_title({"Page2": {"Name": ""}}, "", 42) == "OutSystems Page 42"


def test_empty_page_yields_empty_text() -> None:
    assert extract_page_text({"Page2": {"Name": ""}}) == ""
    assert extract_page_text(_page("   ")) == ""


def test_ignores_non_text1_string_fields() -> None:
    # Only PageWidgetItem.Text1 is content; other strings must be ignored.
    data = {
        "Page2": {"Name": "x", "GUID": "should-not-appear", "CustomURL": "nope"},
        "Junk": {"SomeOtherField": "also should not appear"},
        "StrPageSectionRowList": {
            "List": [
                {
                    "StrPageSectionList": {
                        "List": [
                            {"W": {"List": [{"PageWidgetItem": {"Text1": "real body"}}]}}
                        ]
                    }
                }
            ]
        },
    }
    body = extract_page_text(data)
    assert body == "real body"


# ---- widget classification: text widgets vs document(file) widgets ----------
def _widget(text1: str, page_file_id: str = "0", text2: str = "") -> dict:
    item = {"PageWidgetItem": {"Text1": text1, "PageFileId": page_file_id, "Text2": text2}}
    return {
        "StrPageSectionRowList": {
            "List": [{"StrPageSectionList": {"List": [{"W": {"List": [item]}}]}}]
        }
    }


def test_text_widget_goes_to_html_blobs_not_files() -> None:
    html_blobs: list[str] = []
    files: list[tuple[str, str]] = []
    collect_widgets(_widget("<p>hello world</p>", page_file_id="0"), html_blobs, files)
    assert html_blobs == ["<p>hello world</p>"]
    assert files == []


def test_document_widget_routes_to_files_with_path_and_name() -> None:
    # Document widget: Text1 is a FilePath, Text2 the filename, PageFileId != 0.
    html_blobs: list[str] = []
    files: list[tuple[str, str]] = []
    data = _widget(
        "IC_Content/Docs/conflict-of-interest-policy.pdf",
        page_file_id="1482",
        text2="conflict-of-interest-policy.pdf",
    )
    collect_widgets(data, html_blobs, files)
    assert files == [
        ("IC_Content/Docs/conflict-of-interest-policy.pdf",
         "conflict-of-interest-policy.pdf")
    ]
    # the file path must NOT be treated as body text (latent-bug guard)
    assert html_blobs == []
    assert "IC_Content" not in extract_page_text(data)


def test_document_widget_filename_falls_back_to_path_basename() -> None:
    html_blobs: list[str] = []
    files: list[tuple[str, str]] = []
    collect_widgets(_widget("Lib/Docs/policy.pdf", page_file_id="9"), html_blobs, files)
    assert files == [("Lib/Docs/policy.pdf", "policy.pdf")]


# ---- process-isolated extraction timeout (the fix for GIL-bound parse hangs) --
def test_extract_timeout_returns_empty_and_is_bounded(monkeypatch) -> None:
    """A pathological parse that never returns must be hard-killed within the
    timeout and yield "" — proving the index attempt can't be frozen by one file."""
    def _hang(**_kwargs):
        time.sleep(60)
        return "never"

    monkeypatch.setattr(os_connector, "extract_file_text", _hang)
    start = time.time()
    out = _extract_text_with_timeout("bad.pdf", b"data", timeout=2)
    elapsed = time.time() - start
    assert out == ""
    assert elapsed < 10  # killed near the 2s bound, not hung


def test_extract_fast_path_returns_text(monkeypatch) -> None:
    monkeypatch.setattr(os_connector, "extract_file_text", lambda **_k: "hello body")
    assert _extract_text_with_timeout("a.pdf", b"data", timeout=10) == "hello body"


# ---- long-token sanitizer (prevents O(n^2) tokenizer hang on table dumps) -----
def test_break_long_tokens_bounds_runs() -> None:
    from danswer.connectors.outsystems.connector import _break_long_tokens
    out = _break_long_tokens("X" * 500_000)
    assert max(len(w) for w in out.split()) <= 80


def test_break_long_tokens_leaves_normal_text() -> None:
    from danswer.connectors.outsystems.connector import _break_long_tokens
    s = "Reporting an ethics concern: email ethics@uipath.com for help."
    assert _break_long_tokens(s) == s


# ---- section splitting (keeps giant docs indexable without a tokenizer hang) --
def test_split_sections_bounds_size_and_keeps_content() -> None:
    from danswer.connectors.outsystems.connector import _split_sections, _MAX_SECTION_CHARS
    secs = _split_sections("word " * 200_000, "http://x", header="big.pdf")
    assert len(secs) > 1
    assert all(len(s.text) <= _MAX_SECTION_CHARS for s in secs)
    assert secs[0].text.startswith("big.pdf")
    assert all(s.link == "http://x" for s in secs)


def test_split_sections_small_text_single_section() -> None:
    from danswer.connectors.outsystems.connector import _split_sections
    secs = _split_sections("just a little text", "http://x")
    assert len(secs) == 1 and secs[0].text == "just a little text"
