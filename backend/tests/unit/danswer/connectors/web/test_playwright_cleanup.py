"""Tests that the web connector always stops Playwright.

A sync_playwright instance that isn't stopped leaves its asyncio loop marked
running on the thread, so the next start_playwright() on that thread fails with
"using Playwright Sync API inside the asyncio loop". Dask workers run one thread
each, so a single leak broke every later web connector on the pod. These drive
load_from_state with a fake browser and check every started instance ends up
stopped, however the crawl ends.
"""
from typing import Any

import pytest

from danswer.connectors.web import connector as web_connector
from danswer.connectors.web.connector import start_playwright
from danswer.connectors.web.connector import WebConnector


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}


class _FakePage:
    def __init__(self, pages: dict[str, tuple[int, str]]) -> None:
        self._pages = pages
        self.url = ""

    def goto(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.url = url
        return _FakeResponse(self._pages[url][0])

    def content(self) -> str:
        return self._pages[self.url][1]

    def close(self) -> None:
        pass


class _FakePlaywright:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _FakeContext:
    def __init__(self, pages: dict[str, tuple[int, str]]) -> None:
        self._pages = pages

    def new_page(self) -> _FakePage:
        return _FakePage(self._pages)


def _fake_browser(
    monkeypatch: pytest.MonkeyPatch, pages: dict[str, tuple[int, str]]
) -> list[_FakePlaywright]:
    started: list[_FakePlaywright] = []

    def fake_start_playwright() -> tuple[_FakePlaywright, _FakeContext]:
        pw = _FakePlaywright()
        started.append(pw)
        return pw, _FakeContext(pages)

    monkeypatch.setattr(web_connector, "start_playwright", fake_start_playwright)
    monkeypatch.setattr(web_connector, "check_internet_connection", lambda url: None)
    return started


def _html(title: str, body: str = "") -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def test_stops_playwright_when_crawl_finds_no_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The prod case: the only page is skipped (HTTP 4xx), so load_from_state
    # raises RuntimeError — previously without stopping Playwright.
    started = _fake_browser(
        monkeypatch, {"https://site.test/": (403, _html("Just a moment..."))}
    )
    connector = WebConnector(base_url="https://site.test/", web_connector_type="single")

    with pytest.raises(RuntimeError, match="HTTP 403"):
        list(connector.load_from_state())

    assert len(started) == 1
    assert started[0].stopped


def test_stops_instance_restarted_after_a_batch_when_later_pages_yield_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Page A fills a batch (instance 1 stopped, restart flagged); page B starts
    # instance 2 but 404s, so the crawl ends with an empty batch and no error —
    # previously instance 2 leaked silently.
    started = _fake_browser(
        monkeypatch,
        {
            "https://site.test/": (200, _html("A", '<a href="/b">b</a>')),
            "https://site.test/b": (404, _html("Not found")),
        },
    )
    connector = WebConnector(
        base_url="https://site.test/", web_connector_type="recursive", batch_size=1
    )

    batches = list(connector.load_from_state())

    assert [d.id for batch in batches for d in batch] == ["https://site.test/"]
    assert len(started) == 2
    assert all(pw.stopped for pw in started)


def test_start_playwright_stops_instance_if_browser_launch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pw = _FakePlaywright()

    class _FailingChromium:
        def launch(self, **kwargs: Any) -> None:
            raise RuntimeError("launch failed")

    pw.chromium = _FailingChromium()  # type: ignore[attr-defined]

    class _FakeSyncPlaywright:
        def start(self) -> _FakePlaywright:
            return pw

    monkeypatch.setattr(web_connector, "sync_playwright", _FakeSyncPlaywright)

    with pytest.raises(RuntimeError, match="launch failed"):
        start_playwright()
    assert pw.stopped
