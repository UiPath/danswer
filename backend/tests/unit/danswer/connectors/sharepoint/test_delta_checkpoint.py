"""Unit tests for the SharePoint delta-based resumable checkpoint enumeration
(`load_from_checkpoint`) and the delta-item -> Document converter. Pure logic —
network calls (_graph_get, _download_item_bytes, _all_drive_ids,
_populate_sitedata_sites) are stubbed."""
import json

from danswer.connectors.sharepoint.connector import _delta_item_to_document
from danswer.connectors.sharepoint.connector import SCOPE_DOCUMENTS
from danswer.connectors.sharepoint.connector import SCOPE_FULL
from danswer.connectors.sharepoint.connector import SharepointConnector


def _file(id_: str, name: str) -> dict:
    return {
        "id": id_,
        "name": name,
        "webUrl": f"https://x.sharepoint.com/{name}",
        "file": {"mimeType": "text/plain"},
        "lastModifiedDateTime": "2025-01-01T00:00:00Z",
        "lastModifiedBy": {"user": {"displayName": "A", "email": "a@x.com"}},
    }


def _stub_drives_only(conn: SharepointConnector, pages: dict[str, dict]) -> None:
    conn._populate_sitedata_sites = lambda: None  # type: ignore[method-assign]
    conn._all_drive_ids = lambda: ["drv1"]  # type: ignore[method-assign]
    conn._download_item_bytes = (  # type: ignore[method-assign]
        lambda drive_id, item_id, label: b"body-" + item_id.encode()
    )

    def _graph_get(url: str, params: object = None) -> dict:
        # base delta url -> page1; nextLink tokens map to their page
        key = "BASE" if url.endswith("/root/delta") else url
        return pages[key]

    conn._graph_get = _graph_get  # type: ignore[method-assign]


def test_delta_item_to_document() -> None:
    doc = _delta_item_to_document(_file("id1", "note.txt"), b"hello sharepoint")
    assert doc.id == "id1"
    assert doc.semantic_identifier == "note.txt"
    assert "hello sharepoint" in doc.sections[0].text
    assert doc.primary_owners and doc.primary_owners[0].email == "a@x.com"
    assert doc.doc_updated_at is not None


def test_load_from_checkpoint_progression_and_skips() -> None:
    """Fresh crawl: folders/deletions skipped; each finished delta page yields a
    resume-safe checkpoint carrying the NEXT page's cursor; final checkpoint is
    'done'."""
    conn = SharepointConnector(sites=[], scrape_scope=SCOPE_DOCUMENTS)
    conn.batch_size = 10  # larger than any page -> no mid-page split
    pages = {
        "BASE": {
            "value": [
                _file("a", "a.txt"),
                _file("b", "b.txt"),
                {"id": "F", "name": "folder", "folder": {}},  # skipped
            ],
            "@odata.nextLink": "NEXT",
        },
        "NEXT": {
            "value": [
                _file("c", "c.txt"),
                {**_file("d", "d.txt"), "deleted": {"state": "deleted"}},  # skipped
            ]
        },
    }
    _stub_drives_only(conn, pages)

    out = list(conn.load_from_checkpoint(0.0, 9_000_000_000.0, None))
    batches = [[d.id for d in b] for b, _ in out]
    checkpoints = [json.loads(cp) if cp else None for _, cp in out]

    # page1 -> [a,b] (folder skipped); page2 -> [c] (deletion skipped); final []
    assert batches == [["a", "b"], ["c"], []]
    assert checkpoints[0] == {"phase": "drives", "drive_index": 0, "delta_url": "NEXT"}
    assert checkpoints[1] == {"phase": "drives", "drive_index": 0, "delta_url": None}
    assert checkpoints[-1] == {"phase": "done"}


def test_load_from_checkpoint_resumes_from_cursor() -> None:
    """Given a checkpoint pointing at the 2nd page, the crawl resumes there and
    does NOT re-fetch page 1."""
    conn = SharepointConnector(sites=[], scrape_scope=SCOPE_DOCUMENTS)
    conn.batch_size = 10
    pages = {
        "BASE": {"value": [_file("a", "a.txt")], "@odata.nextLink": "NEXT"},
        "NEXT": {"value": [_file("c", "c.txt")]},
    }
    _stub_drives_only(conn, pages)

    resume = json.dumps({"phase": "drives", "drive_index": 0, "delta_url": "NEXT"})
    out = list(conn.load_from_checkpoint(0.0, 9_000_000_000.0, resume))
    ids = [d.id for b, _ in out for d in b]
    assert ids == ["c"]  # page 1 ('a') was NOT re-fetched


def test_load_from_checkpoint_mid_page_batches_have_no_checkpoint() -> None:
    """With batch_size smaller than a page, mid-page batches carry checkpoint=None
    (not a safe resume point); only the page boundary carries a real cursor."""
    conn = SharepointConnector(sites=[], scrape_scope=SCOPE_DOCUMENTS)
    conn.batch_size = 1
    pages = {
        "BASE": {"value": [_file("a", "a.txt"), _file("b", "b.txt")]},  # 1 page, 2 files
    }
    _stub_drives_only(conn, pages)

    out = list(conn.load_from_checkpoint(0.0, 9_000_000_000.0, None))
    # first file: mid-page -> None; page end flushes [b] + real checkpoint; then done
    assert [d.id for d in out[0][0]] == ["a"]
    assert out[0][1] is None  # mid-page batch: no resume point
    assert any(cp is not None for _, cp in out)  # a real checkpoint is emitted
