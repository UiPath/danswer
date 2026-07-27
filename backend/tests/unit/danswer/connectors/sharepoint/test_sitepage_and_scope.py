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


def test_retrieve_all_source_ids_full_scope() -> None:
    """Pruning hook returns drive-item ids + site-page ids (no content), matching
    the ids _fetch_from_sharepoint emits, so deletions are detected."""
    from types import SimpleNamespace

    from danswer.connectors.sharepoint.connector import SiteData

    conn = SharepointConnector(sites=[], scrape_scope=SCOPE_FULL)
    conn.graph_client = object()  # type: ignore[assignment]
    conn.site_data = [
        SiteData(
            url="https://x.sharepoint.com/sites/s",
            folder=None,
            sites=[SimpleNamespace(id="site1")],
            driveitems=[SimpleNamespace(id="fileA"), SimpleNamespace(id="fileB")],
        )
    ]
    # skip network: sites already populated, driveitems already set
    conn._populate_sitedata_sites = lambda: None  # type: ignore[method-assign]
    conn._populate_sitedata_driveitems = lambda start=None, end=None: None  # type: ignore[method-assign]
    conn._fetch_site_page_ids = lambda site_id: iter(["p1", "p2"])  # type: ignore[method-assign]

    ids = conn.retrieve_all_source_ids()
    assert ids == {
        "fileA",
        "fileB",
        "sharepoint_page__p1",
        "sharepoint_page__p2",
    }


def test_retrieve_all_source_ids_documents_scope_skips_pages() -> None:
    from types import SimpleNamespace

    from danswer.connectors.sharepoint.connector import SiteData

    conn = SharepointConnector(sites=[], scrape_scope=SCOPE_DOCUMENTS)
    conn.graph_client = object()  # type: ignore[assignment]
    conn.site_data = [
        SiteData(
            url=None,
            folder=None,
            sites=[SimpleNamespace(id="site1")],
            driveitems=[SimpleNamespace(id="fileA")],
        )
    ]
    conn._populate_sitedata_sites = lambda: None  # type: ignore[method-assign]
    conn._populate_sitedata_driveitems = lambda start=None, end=None: None  # type: ignore[method-assign]

    called = {"pages": False}

    def _no_pages(site_id: str) -> object:
        called["pages"] = True
        return iter([])

    conn._fetch_site_page_ids = _no_pages  # type: ignore[method-assign]
    assert conn.retrieve_all_source_ids() == {"fileA"}
    assert called["pages"] is False  # documents scope never lists pages


def test_incremental_poll_routes_to_per_page_expansion() -> None:
    """With a time window set, _fetch_site_pages must use the metadata-then-
    expand-changed path (efficient), not the bulk canvas download."""
    conn = SharepointConnector(sites=[], scrape_scope=SCOPE_FULL)
    seen: dict[str, object] = {}

    def _individually(base, start, end, skip):  # type: ignore[no-untyped-def]
        seen["called"] = (start, end)
        return iter([])

    conn._fetch_site_pages_individually = _individually  # type: ignore[method-assign]
    list(conn._fetch_site_pages("site1", start=datetime(2026, 1, 1), end=None))
    assert seen.get("called") == (datetime(2026, 1, 1), None)


def test_convert_driveitem_builds_document_from_raw_bytes() -> None:
    """Content is downloaded separately (raw Graph) and passed in as bytes; the
    builder just extracts text and assembles the Document — no office365
    get_content(), so no JSON-deserialization object blow-up."""
    from types import SimpleNamespace

    from danswer.connectors.sharepoint.connector import (
        _convert_driveitem_to_document,
    )

    item = SimpleNamespace(
        id="itm1",
        name="note.txt",
        web_url="https://x.sharepoint.com/sites/s/note.txt",
        last_modified_datetime=datetime(2026, 7, 10, 8, 0),
        last_modified_by=SimpleNamespace(
            user=SimpleNamespace(displayName="Alex", email="a@uipath.com")
        ),
    )
    doc = _convert_driveitem_to_document(item, b"hello sharepoint")  # type: ignore[arg-type]
    assert doc.id == "itm1"
    assert doc.semantic_identifier == "note.txt"
    assert "hello sharepoint" in doc.sections[0].text
    assert doc.primary_owners and doc.primary_owners[0].email == "a@uipath.com"


def test_download_content_returns_none_when_ids_missing() -> None:
    """No drive id (context or parentReference) / no item id -> skip (return None)
    without any network call, so the caller drops the item instead of erroring."""
    from types import SimpleNamespace

    conn = SharepointConnector(sites=[], scrape_scope=SCOPE_FULL)
    # no context drive_id, no parentReference, no id -> None
    item = SimpleNamespace(id=None, parent_reference=None, web_url="w")
    assert conn._download_driveitem_content(item) is None  # type: ignore[arg-type]
    # a context drive_id is supplied but the item has no id -> still None
    item2 = SimpleNamespace(id=None, parent_reference=None, web_url="w")
    assert conn._download_driveitem_content(item2, "drv-1") is None  # type: ignore[arg-type]


def test_iter_driveitems_streams_per_library_without_retaining() -> None:
    """Full-scope content crawl must stream items one library at a time and NOT
    stash them all on `self.site_data[].driveitems`. That retained list (plus the
    office365 object graph hanging off it) was the live heap the cyclic GC
    rescanned every pass, making batch time grow 130s -> 220s -> 583s."""
    from types import SimpleNamespace

    from danswer.connectors.sharepoint.connector import SiteData

    conn = SharepointConnector(sites=[], scrape_scope=SCOPE_FULL)
    drives = [
        SimpleNamespace(name="Documents", id="drv-Documents"),
        SimpleNamespace(name="Onboarding", id="drv-Onboarding"),
    ]
    site = SimpleNamespace(
        id="s1",
        drives=SimpleNamespace(
            get=lambda: SimpleNamespace(execute_query=lambda: drives)
        ),
    )
    element = SiteData(url="u", folder=None, sites=[site], driveitems=[])

    visited: list[str] = []

    def fake_drive_files(drive: object, folder: object, filter_str: str) -> list:
        visited.append(drive.name)  # type: ignore[attr-defined]
        return [
            SimpleNamespace(id=f"{drive.name}-a"),  # type: ignore[attr-defined]
            SimpleNamespace(id=f"{drive.name}-b"),  # type: ignore[attr-defined]
        ]

    conn._drive_files = fake_drive_files  # type: ignore[method-assign]

    pairs = list(conn._iter_driveitems(element))
    ids = [it.id for it, _ in pairs]
    drive_ids = [did for _, did in pairs]
    assert ids == ["Documents-a", "Documents-b", "Onboarding-a", "Onboarding-b"]
    # drive_id comes from the enumeration context, NOT item.parentReference
    # (which a filtered get_files() leaves unpopulated) — this is what makes
    # the raw content download work for filtered/backfill polls.
    assert drive_ids == [
        "drv-Documents",
        "drv-Documents",
        "drv-Onboarding",
        "drv-Onboarding",
    ]
    assert visited == ["Documents", "Onboarding"]  # each library visited once
    assert element.driveitems == []  # nothing retained on the connector instance


def test_poll_source_passes_tz_aware_window() -> None:
    """poll_source must hand _fetch_from_sharepoint tz-AWARE datetimes; naive
    ones crashed site-page filtering (naive vs aware comparison) and aborted the
    whole site-pages fetch."""
    conn = SharepointConnector(sites=[], scrape_scope=SCOPE_FULL)
    captured: dict[str, datetime] = {}

    def _capture(start=None, end=None):  # type: ignore[no-untyped-def]
        captured["start"] = start
        captured["end"] = end
        return iter([])

    conn._fetch_from_sharepoint = _capture  # type: ignore[method-assign]
    list(conn.poll_source(0.0, 1_800_000_000.0))
    assert captured["start"].tzinfo is not None
    assert captured["end"].tzinfo is not None


def test_is_invalid_request_detects_corrupt_canvas() -> None:
    class _Resp:
        def __init__(self, code: str | None) -> None:
            self._code = code

        def json(self) -> dict:
            return {"error": {"code": self._code}} if self._code else {}

    assert _is_invalid_request(_Resp("invalidRequest")) is True
    assert _is_invalid_request(_Resp("throttled")) is False
    assert _is_invalid_request(None) is False
