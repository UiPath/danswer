"""Unit tests for the SharePoint connector's site-page extraction, scrape-scope
option, and the reliability helpers (backoff / retryable detection). Pure logic
— no network."""
from datetime import datetime
from datetime import timezone

from danswer.connectors.sharepoint.connector import _backoff_seconds
from danswer.connectors.sharepoint.connector import _convert_sitepage_to_document
from danswer.connectors.sharepoint.connector import _extract_sitepage_text
from danswer.connectors.sharepoint.connector import _is_invalid_request
from danswer.connectors.sharepoint.connector import _site_page_in_time_window
from danswer.connectors.sharepoint.connector import SCOPE_DOCUMENTS
from danswer.connectors.sharepoint.connector import SCOPE_FULL
from danswer.connectors.sharepoint.connector import SharepointConnector


# --- a realistic modern-page canvas payload --------------------------------

_PAGE = {
    "id": "abc123",
    "name": "Enterprise-Success-Team.aspx",
    "title": "Client Success Team Using Gainsight",
    "description": "How the team tracks platform utilization.",
    "webUrl": "https://uipath.sharepoint.com/sites/TAM/SitePages/Enterprise.aspx",
    "lastModifiedDateTime": "2026-07-10T08:00:00Z",
    "createdBy": {"user": {"displayName": "Alex Iordan", "email": "a@uipath.com"}},
    "canvasLayout": {
        "horizontalSections": [
            {
                "columns": [
                    {
                        "webparts": [
                            {
                                "@odata.type": "#microsoft.graph.textWebPart",
                                "innerHtml": (
                                    "<h2>Overview</h2><p>Track <b>platform "
                                    "utilization</b> across accounts.</p>"
                                    "<ul><li>Adoption</li><li>Sentiment</li></ul>"
                                ),
                            },
                            {
                                "@odata.type": "#microsoft.graph.standardWebPart",
                                "data": {
                                    "title": "Gainsight Dashboard",
                                    "description": "Live health scores.",
                                    "serverProcessedContent": {
                                        "searchablePlainTexts": [
                                            {
                                                "key": "title",
                                                "value": "Health Framework",
                                            },
                                            {
                                                "key": "body",
                                                "value": "AMER rollout notes",
                                            },
                                        ]
                                    },
                                },
                            },
                        ]
                    }
                ]
            }
        ]
    },
}


def test_extract_sitepage_text_pulls_all_webpart_content() -> None:
    text = _extract_sitepage_text(_PAGE)
    # title + description headers
    assert "# Client Success Team Using Gainsight" in text
    assert "How the team tracks platform utilization." in text
    # text webpart: html stripped, structure preserved
    assert "## Overview" in text
    assert "Track platform utilization across accounts." in text
    assert "• Adoption" in text and "• Sentiment" in text
    assert "<" not in text  # no raw HTML tags leak through
    # standard webpart: searchable texts + title + description
    assert "## Health Framework" in text
    assert "AMER rollout notes" in text
    assert "Gainsight Dashboard" in text
    assert "Live health scores." in text


def test_extract_sitepage_text_falls_back_to_title_when_empty() -> None:
    assert _extract_sitepage_text({"title": "Just A Title"}) == "# Just A Title"
    assert _extract_sitepage_text({"title": "T", "canvasLayout": {}}) == "# T"


def test_convert_sitepage_to_document() -> None:
    doc = _convert_sitepage_to_document(_PAGE, site_name="Client Success")
    assert doc.id == "sharepoint_page__abc123"
    assert doc.semantic_identifier == "Client Success Team Using Gainsight"
    assert doc.sections[0].link == _PAGE["webUrl"]
    assert "platform utilization" in doc.sections[0].text
    assert doc.doc_updated_at == datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)
    assert doc.primary_owners and doc.primary_owners[0].email == "a@uipath.com"
    assert doc.metadata.get("type") == "site_page"


def test_site_page_time_window() -> None:
    page = {"lastModifiedDateTime": "2026-07-10T00:00:00Z"}
    assert _site_page_in_time_window(page, None, None) is True
    inside = datetime(2026, 7, 1, tzinfo=timezone.utc)
    after = datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert _site_page_in_time_window(page, inside, after) is True
    assert (
        _site_page_in_time_window(page, after, None) is False
    )  # modified before start
    # missing timestamp -> included (can't exclude what we can't date)
    assert _site_page_in_time_window({}, inside, after) is True


def test_scrape_scope_normalization() -> None:
    assert SharepointConnector(sites=[]).scrape_scope == SCOPE_DOCUMENTS  # default
    assert SharepointConnector(sites=[], scrape_scope="FULL").scrape_scope == SCOPE_FULL
    assert (
        SharepointConnector(sites=[], scrape_scope="bogus").scrape_scope
        == SCOPE_DOCUMENTS
    )


def test_backoff_honors_retry_after_then_falls_back() -> None:
    # explicit Retry-After wins (capped at 60)
    assert _backoff_seconds(0, "12") == 12.0
    assert _backoff_seconds(0, "999") == 60.0
    # no header -> bounded jittered backoff within [base/2, base]
    for attempt, base in [(0, 5), (1, 10), (2, 20), (5, 30)]:
        w = _backoff_seconds(attempt, None)
        assert base / 2 <= w <= base


def test_is_invalid_request_detects_corrupt_canvas() -> None:
    class _Resp:
        def __init__(self, code: str | None) -> None:
            self._code = code

        def json(self) -> dict:
            return {"error": {"code": self._code}} if self._code else {}

    assert _is_invalid_request(_Resp("invalidRequest")) is True
    assert _is_invalid_request(_Resp("throttled")) is False
    assert _is_invalid_request(None) is False
