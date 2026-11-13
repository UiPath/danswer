"""Every exit from WebConnector.load_from_state must stop Playwright.

A dask worker runs all jobs on one thread; a sync_playwright() left running
there makes every later web attempt on that worker fail with "Playwright Sync
API inside the asyncio loop" (prod, 2026-11-02 → 11-13).
"""
from collections.abc import Generator
from typing import Any
from typing import cast
from unittest.mock import MagicMock

import pytest

from danswer.connectors.web import connector as web


def _fake_browser(monkeypatch: pytest.MonkeyPatch, status: int) -> list[MagicMock]:
    started: list[MagicMock] = []

    def fake_start() -> tuple[MagicMock, MagicMock]:
        pw = MagicMock(name=f"playwright{len(started)}")
        started.append(pw)
        context = MagicMock()
        page = context.new_page.return_value
        page.goto.return_value.status = status
        page.url = "https://example.com/"
        page.content.return_value = "<html><title>t</title><body>hello</body></html>"
        return pw, context

    monkeypatch.setattr(web, "start_playwright", fake_start)
    monkeypatch.setattr(web, "check_internet_connection", lambda url: None)
    monkeypatch.setattr(web, "protected_url_check", lambda url: None)
    return started


def _connector() -> web.WebConnector:
    return web.WebConnector(
        "https://example.com/",
        web_connector_type=web.WEB_CONNECTOR_VALID_SETTINGS.SINGLE.value,
    )


def test_stops_playwright_when_no_page_is_indexed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # e.g. Cloudflare 403 on every page -> RuntimeError, browser must not leak
    started = _fake_browser(monkeypatch, status=403)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        list(_connector().load_from_state())
    assert started and all(pw.stop.called for pw in started)


def test_stops_playwright_when_consumer_abandons_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _fake_browser(monkeypatch, status=200)
    conn = _connector()
    conn.batch_size = 1
    gen = cast(Generator, conn.load_from_state())
    next(gen)  # got a batch; e.g. "connector disabled mid run" -> abandoned
    gen.close()
    assert started and all(pw.stop.called for pw in started)


def test_stops_playwright_on_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    started = _fake_browser(monkeypatch, status=200)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise KeyError("parser bug")

    monkeypatch.setattr(web, "web_html_cleanup", boom)
    monkeypatch.setattr(web, "WEB_CONNECTOR_MAX_RETRIES", 1)
    with pytest.raises(RuntimeError):
        list(_connector().load_from_state())
    assert started and all(pw.stop.called for pw in started)


def test_start_playwright_stops_if_launch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pw = MagicMock()
    pw.chromium.launch.side_effect = RuntimeError("no chromium")
    monkeypatch.setattr(web, "sync_playwright", lambda: MagicMock(start=lambda: pw))
    with pytest.raises(RuntimeError, match="no chromium"):
        web.start_playwright()
    pw.stop.assert_called_once()
