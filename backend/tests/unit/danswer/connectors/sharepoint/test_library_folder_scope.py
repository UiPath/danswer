"""Unit tests for library/folder-scoped SharePoint URLs (upstream-Onyx-style
semantics): share-link token stripping, /sites|teams|personal parsing with
library + nested folder path, drive resolution by name, BFS /children
enumeration, the scoped checkpoint crawl, and scoped pruning ids. Pure logic —
network calls are stubbed."""
from types import SimpleNamespace

import pytest

from danswer.connectors.sharepoint.connector import SCOPE_FULL
from danswer.connectors.sharepoint.connector import SharepointConnector
from danswer.connectors.sharepoint.connector import SiteData


# --- URL parsing -------------------------------------------------------------


def test_parse_site_only_unchanged() -> None:
    conn = SharepointConnector(sites=["https://uipath.sharepoint.com/sites/Product"])
    element = conn.site_data[0]
    assert element.url == "https://uipath.sharepoint.com/sites/Product"
    assert element.drive_name is None
    assert element.folder_path is None


def test_parse_library_and_nested_folder() -> None:
    conn = SharepointConnector(
        sites=[
            "https://uipath.sharepoint.com/sites/Product/Shared%20Documents/"
            "Platform%20(Base,%20Cloud%20and%20AS)/_Platform%20Base/Governance/"
            "Darwin%20-%20Help%20AI%20Trust%20Layer"
        ]
    )
    element = conn.site_data[0]
    assert element.url == "https://uipath.sharepoint.com/sites/Product"
    assert element.drive_name == "Shared Documents"
    assert element.folder_path == (
        "Platform (Base, Cloud and AS)/_Platform Base/Governance/"
        "Darwin - Help AI Trust Layer"
    )


def test_parse_strips_path_style_share_link_tokens() -> None:
    conn = SharepointConnector(
        sites=[
            "https://uipath.sharepoint.com/:f:/r/sites/Product/"
            "Shared%20Documents/SomeFolder?csf=1&web=1"
        ]
    )
    element = conn.site_data[0]
    assert element.url == "https://uipath.sharepoint.com/sites/Product"
    assert element.drive_name == "Shared Documents"
    assert element.folder_path == "SomeFolder"


def test_parse_teams_and_personal() -> None:
    conn = SharepointConnector(
        sites=[
            "https://uipath.sharepoint.com/teams/eng-platform",
            "https://uipath.sharepoint.com/personal/jane_doe_uipath_com/Documents",
        ]
    )
    assert conn.site_data[0].url == "https://uipath.sharepoint.com/teams/eng-platform"
    assert conn.site_data[1].drive_name == "Documents"


def test_parse_rejects_opaque_share_token() -> None:
    # /:f:/s/<token> carries no path — previously it yielded empty site_data
    # and a silent 0-doc "success".
    with pytest.raises(ValueError, match="Unrecognized SharePoint URL"):
        SharepointConnector(
            sites=[
                "https://uipath.sharepoint.com/:f:/s/Product/"
                "IgC6NlS-LnzxSaGlOC8wM205AfA9FCf2P4qCNOp31FmcptY?e=PB0pfe"
            ]
        )


def test_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="Unrecognized SharePoint URL"):
        SharepointConnector(sites=["https://uipath.sharepoint.com/nothing/here"])


# --- drive resolution ---------------------------------------------------------


def _site_with_drives(*drives: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        drives=SimpleNamespace(
            get=lambda: SimpleNamespace(execute_query=lambda: list(drives))
        )
    )


def test_resolve_drive_id_maps_shared_documents() -> None:
    site = _site_with_drives(
        SimpleNamespace(name="Documents", id="drv-default"),
        SimpleNamespace(name="Onboarding", id="drv-onb"),
    )
    # browser spelling of the default library resolves to Graph's "Documents"
    assert SharepointConnector._resolve_drive_id(site, "Shared Documents") == (
        "drv-default"
    )
    assert SharepointConnector._resolve_drive_id(site, "onboarding") == "drv-onb"


def test_resolve_drive_id_unknown_raises_with_names() -> None:
    site = _site_with_drives(SimpleNamespace(name="Documents", id="d1"))
    with pytest.raises(ValueError, match="Available libraries.*Documents"):
        SharepointConnector._resolve_drive_id(site, "Nope")


# --- BFS folder enumeration ----------------------------------------------------


def _scoped_connector() -> SharepointConnector:
    conn = SharepointConnector(
        sites=["https://uipath.sharepoint.com/sites/Product/Shared%20Documents/A/B"]
    )
    conn.graph_client = object()  # type: ignore[assignment]
    return conn


def test_iter_folder_items_bfs() -> None:
    conn = _scoped_connector()
    base = "https://graph.microsoft.com/v1.0/drives/drv1"
    pages = {
        f"{base}/root:/A/B:/children": {
            "value": [
                {"id": "f1", "name": "a.pdf", "file": {}},
                {"id": "sub", "name": "Sub", "folder": {"childCount": 1}},
            ],
            "@odata.nextLink": "NEXT1",
        },
        "NEXT1": {"value": [{"id": "f2", "name": "b.docx", "file": {}}]},
        f"{base}/items/sub/children": {
            "value": [{"id": "f3", "name": "c.txt", "file": {}}]
        },
    }
    conn._graph_get = lambda url, params=None: pages[url]  # type: ignore[method-assign]
    items = list(conn._iter_folder_items("drv1", "A/B"))
    # breadth-first: current folder's pages fully drained before the subfolder
    assert [i["id"] for i in items] == ["f1", "f2", "f3"]


def test_iter_folder_items_encodes_path() -> None:
    conn = _scoped_connector()
    seen: list[str] = []

    def _graph_get(url: str, params: object = None) -> dict:
        seen.append(url)
        return {"value": []}

    conn._graph_get = _graph_get  # type: ignore[method-assign]
    list(conn._iter_folder_items("drv1", "Platform (Base)/_Base/Darwin - Help"))
    assert seen == [
        "https://graph.microsoft.com/v1.0/drives/drv1"
        "/root:/Platform%20%28Base%29/_Base/Darwin%20-%20Help:/children"
    ]


# --- scoped checkpoint crawl ----------------------------------------------------


def test_scoped_checkpoint_crawl() -> None:
    conn = _scoped_connector()
    conn._populate_sitedata_sites = lambda: None  # type: ignore[method-assign]
    conn._delta_roots = lambda: [("drv1", "A/B")]  # type: ignore[method-assign]
    conn._iter_folder_items = (  # type: ignore[method-assign]
        lambda drive_id, folder_path, select=None: iter(
            [
                {
                    "id": "doc1",
                    "name": "note.txt",
                    "webUrl": "https://x/note.txt",
                    "file": {"mimeType": "text/plain"},
                    "lastModifiedDateTime": "2025-06-01T00:00:00Z",
                    "lastModifiedBy": {"user": {"displayName": "A", "email": "a@x"}},
                },
                {"id": "pkg", "name": "notebook"},  # no file facet -> skipped
            ]
        )
    )
    conn._download_item_bytes = (  # type: ignore[method-assign]
        lambda drive_id, item_id, label: b"hello"
    )
    out = list(conn.load_from_checkpoint(0, 2_000_000_000, None))
    docs = [d for batch, _ in out for d in batch]
    assert [d.id for d in docs] == ["doc1"]
    # subtree completion emits a resume point that advances past this root
    checkpoints = [cp for _, cp in out if cp is not None]
    assert '"drive_index": 1' in checkpoints[0]


# --- scoped pruning --------------------------------------------------------------


def test_retrieve_all_source_ids_scoped() -> None:
    conn = _scoped_connector()
    site = _site_with_drives(SimpleNamespace(name="Documents", id="drv1"))
    conn.site_data = [
        SiteData(
            url="https://uipath.sharepoint.com/sites/Product",
            folder=None,
            sites=[site],
            driveitems=[],
            drive_name="Shared Documents",
            folder_path="A/B",
        )
    ]
    conn.scrape_scope = SCOPE_FULL  # must NOT add site pages for scoped elements
    conn._populate_sitedata_sites = lambda: None  # type: ignore[method-assign]
    conn._iter_folder_items = (  # type: ignore[method-assign]
        lambda drive_id, folder_path, select=None: iter(
            [
                {"id": "a", "file": {}},
                {"id": "b", "file": {}},
                {"id": "x", "package": {}},  # not a file -> excluded
            ]
        )
    )
    assert conn.retrieve_all_source_ids() == {"a", "b"}
