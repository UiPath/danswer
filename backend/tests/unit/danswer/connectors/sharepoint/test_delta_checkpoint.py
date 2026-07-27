"""Unit tests for the SharePoint delta-based resumable checkpoint enumeration
(`load_from_checkpoint`), the delta-item -> Document converter, and the
allow-list + size-cap file filter. Pure logic — network calls (_graph_get,
_download_item_bytes, _download_item_to_file, _all_drive_ids,
_populate_sitedata_sites) are stubbed."""
import io
import json
import os
from datetime import datetime
from datetime import timezone

from danswer.connectors.sharepoint import connector as sp
from danswer.connectors.sharepoint.connector import _DEFAULT_FILE_CAP_BYTES
from danswer.connectors.sharepoint.connector import _delta_item_to_document
from danswer.connectors.sharepoint.connector import _index_decision
from danswer.connectors.sharepoint.connector import _LARGE_FILE_CAP_BYTES
from danswer.connectors.sharepoint.connector import SCOPE_DOCUMENTS
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
    conn.graph_client = object()  # truthy: pass the missing-credential guard
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
    doc = _delta_item_to_document(
        _file("id1", "note.txt"), io.BytesIO(b"hello sharepoint")
    )
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
        "BASE": {
            "value": [_file("a", "a.txt"), _file("b", "b.txt")]
        },  # 1 page, 2 files
    }
    _stub_drives_only(conn, pages)

    out = list(conn.load_from_checkpoint(0.0, 9_000_000_000.0, None))
    # first file: mid-page -> None; page end flushes [b] + real checkpoint; then done
    assert [d.id for d in out[0][0]] == ["a"]
    assert out[0][1] is None  # mid-page batch: no resume point
    assert any(cp is not None for _, cp in out)  # a real checkpoint is emitted


# --- allow-list + size-cap filter -------------------------------------------


def test_index_decision_allow_list_and_caps() -> None:
    assert _index_decision("a.pdf", 1000)[0] is True
    assert _index_decision("a.docx", 1000)[0] is True
    # disallowed extensions (media / data / spreadsheets / no-extension) skipped
    assert _index_decision("movie.mp4", 1000)[0] is False
    assert _index_decision("dump.csv", 1000)[0] is False  # csv excluded
    assert _index_decision("data.json", 1000)[0] is False
    assert _index_decision("sheet.xlsx", 1000)[0] is False  # spreadsheets excluded
    assert _index_decision("book.epub", 1000)[0] is False  # epub excluded
    assert _index_decision("noextension", 1000)[0] is False
    # default 25MB cap for non-pptx
    assert _index_decision("a.pdf", _DEFAULT_FILE_CAP_BYTES + 1)[0] is False
    # .pptx gets the larger (150MB) cap, so it survives past the default cap
    assert _index_decision("deck.pptx", _DEFAULT_FILE_CAP_BYTES + 1)[0] is True
    assert _index_decision("deck.pptx", _LARGE_FILE_CAP_BYTES + 1)[0] is False


def test_index_decision_pptx_age_cutoff() -> None:
    old = datetime(2021, 6, 1, tzinfo=timezone.utc)
    new = datetime(2025, 6, 1, tzinfo=timezone.utc)
    # old decks skipped; 2025+ decks kept; the cutoff applies to pptx only
    assert _index_decision("deck.pptx", 1000, old)[0] is False
    assert _index_decision("deck.pptx", 1000, new)[0] is True
    assert _index_decision("report.pdf", 1000, old)[0] is True  # non-pptx unaffected
    # no modified date -> cutoff can't apply, deck is kept (size-gated only)
    assert _index_decision("deck.pptx", 1000, None)[0] is True


def test_build_delta_document_streams_pptx_to_disk(monkeypatch) -> None:
    """.pptx routes through the disk-streaming path (never _download_item_bytes)
    and the scratch temp file is removed afterwards."""
    conn = SharepointConnector(sites=[], scrape_scope=SCOPE_DOCUMENTS)
    seen: dict[str, str] = {}

    def _to_file(drive_id: str, item_id: str, label: str, dest_path: str) -> str:
        seen["path"] = dest_path
        with open(dest_path, "wb") as f:
            f.write(b"stub-deck-bytes")
        return dest_path

    def _bytes(*a: object, **k: object) -> bytes:
        raise AssertionError("pptx must stream to disk, not load bytes into RAM")

    conn._download_item_to_file = _to_file  # type: ignore[method-assign]
    conn._download_item_bytes = _bytes  # type: ignore[method-assign]
    # avoid needing a real .pptx zip: stub the converter to read the handle
    monkeypatch.setattr(
        sp, "_delta_item_to_document", lambda item, file: (file.read(), item["id"])[1]
    )

    result = conn._build_delta_document("drv1", _file("deck", "deck.pptx"))
    assert result == "deck"
    assert seen["path"] and not os.path.exists(seen["path"])  # cleaned up


def test_build_delta_document_keeps_small_files_in_memory() -> None:
    """Non-pptx allowed files use the in-memory path (no disk streaming)."""
    conn = SharepointConnector(sites=[], scrape_scope=SCOPE_DOCUMENTS)
    conn._download_item_bytes = (  # type: ignore[method-assign]
        lambda drive_id, item_id, label: b"hello body"
    )

    def _no_stream(*a: object, **k: object) -> str:
        raise AssertionError("small files must not stream to disk")

    conn._download_item_to_file = _no_stream  # type: ignore[method-assign]
    doc = conn._build_delta_document("drv1", _file("t", "note.txt"))
    assert doc is not None and doc.id == "t"
    assert "hello body" in doc.sections[0].text


def test_load_from_checkpoint_skips_disallowed_and_oversized(monkeypatch) -> None:
    """The delta loop drops disallowed extensions and oversized files BEFORE any
    download; only allow-listed, within-cap items are emitted."""
    conn = SharepointConnector(sites=[], scrape_scope=SCOPE_DOCUMENTS)
    conn.batch_size = 10
    conn.graph_client = object()  # truthy: pass the missing-credential guard
    pages = {
        "BASE": {
            "value": [
                _file("ok", "ok.txt"),
                _file("vid", "movie.mp4"),  # disallowed ext -> skip
                {
                    **_file("big", "big.pdf"),
                    "size": _DEFAULT_FILE_CAP_BYTES + 1,
                },  # skip
                _file("deck", "deck.pptx"),  # allowed (pptx disk path)
            ]
        },
    }
    conn._populate_sitedata_sites = lambda: None  # type: ignore[method-assign]
    conn._all_drive_ids = lambda: ["drv1"]  # type: ignore[method-assign]
    conn._graph_get = lambda url, params=None: pages["BASE"]  # type: ignore[method-assign]
    conn._download_item_bytes = (  # type: ignore[method-assign]
        lambda drive_id, item_id, label: b"body"
    )

    def _to_file(drive_id: str, item_id: str, label: str, dest_path: str) -> str:
        with open(dest_path, "wb") as f:
            f.write(b"deck")
        return dest_path

    conn._download_item_to_file = _to_file  # type: ignore[method-assign]
    # stub converter so we don't need a real pptx/pdf; key doc by item id
    monkeypatch.setattr(
        sp,
        "_delta_item_to_document",
        lambda item, file: (
            file.read(),
            sp.Document(
                id=item["id"],
                sections=[sp.Section(link=item.get("webUrl"), text="x")],
                source=sp.DocumentSource.SHAREPOINT,
                semantic_identifier=item.get("name"),
                metadata={},
            ),
        )[1],
    )

    out = list(conn.load_from_checkpoint(0.0, 9_000_000_000.0, None))
    ids = [d.id for b, _ in out for d in b]
    assert ids == ["ok", "deck"]  # mp4 + oversized pdf were skipped
