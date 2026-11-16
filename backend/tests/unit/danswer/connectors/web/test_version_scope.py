"""uipath_latest_versions crawls must stay scoped to ALL expanded versions.

recursive_prefixes used to be the same list object as the crawl queue, so
popping/pushing URLs rewrote the allowed scope and a two-version Studio crawl
stopped after ~9 pages (prod, 2026-11-14).
"""
from typing import Any
from unittest.mock import MagicMock

import pytest

from danswer.connectors.web import connector as web

ROOT = "https://docs.uipath.com/studio/standalone"
VERSIONS = [f"{ROOT}/2026.10", f"{ROOT}/2025.10"]
# Every page links to every page of both versions (like the docs sidebar).
PAGES = [f"{v}/user-guide/p{i}" for v in VERSIONS for i in range(5)]


def _fake_site(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    visited: list[str] = []
    links = "".join(f'<a href="{p}">x</a>' for p in PAGES)
    html = f"<html><title>t</title><body>{links}</body></html>"

    def fake_start() -> tuple[MagicMock, MagicMock]:
        context = MagicMock()

        def new_page() -> MagicMock:
            page = MagicMock()

            def goto(url: str, **kwargs: Any) -> MagicMock:
                visited.append(url)
                page.url = url
                resp = MagicMock()
                resp.status = 200
                return resp

            page.goto.side_effect = goto
            page.content.return_value = html
            return page

        context.new_page.side_effect = new_page
        return MagicMock(), context

    monkeypatch.setattr(web, "start_playwright", fake_start)
    monkeypatch.setattr(web, "check_internet_connection", lambda url: None)
    monkeypatch.setattr(web, "protected_url_check", lambda url: None)
    monkeypatch.setattr(
        web,
        "get_uipath_docs_version_base_urls",
        lambda base_url, max_versions=2: list(VERSIONS),
    )
    return visited


def _connector() -> web.WebConnector:
    return web.WebConnector(
        f"{ROOT}/",
        web_connector_type=web.WEB_CONNECTOR_VALID_SETTINGS.RECURSIVE.value,
        uipath_latest_versions=True,
        max_versions=2,
    )


def test_crawls_every_page_of_every_expanded_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visited = _fake_site(monkeypatch)
    conn = _connector()
    docs = [d for batch in conn.load_from_state() for d in batch]
    assert set(PAGES) <= set(visited)
    assert {d.id for d in docs} == set(VERSIONS) | set(PAGES)
    # scope is not consumed by the crawl
    assert conn.recursive_prefixes == VERSIONS


def test_connector_instance_can_crawl_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    # e.g. prune reuses the same config; the seed list must not be drained
    _fake_site(monkeypatch)
    conn = _connector()
    first = [d for b in conn.load_from_state() for d in b]
    second = [d for b in conn.load_from_state() for d in b]
    assert len(first) == len(second) == len(VERSIONS) + len(PAGES)
