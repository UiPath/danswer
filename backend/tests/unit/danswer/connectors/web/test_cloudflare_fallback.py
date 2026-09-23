"""Tests for the web connector's Cloudflare bot-challenge fallback.

Cloudflare serves a 403 "Just a moment..." page (marked `cf-mitigated:
challenge`) to headless Chromium on some sites (e.g. w3.org), and its JS rewrites
page.url to a one-time `?__cf_chl_rt_tk=` URL. The connector detects that and
re-fetches the page without the browser. These guard the detection and the
fallback's accept/reject rules.
"""
from typing import Any

import pytest
import requests

from danswer.connectors.web import connector as web_connector
from danswer.connectors.web.connector import fetch_html_without_browser
from danswer.connectors.web.connector import is_cloudflare_challenge


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        content_type: str = "text/html; charset=utf-8",
        text: str = "<html><title>Real page</title></html>",
        url: str = "https://www.w3.org/TR/WCAG22/",
    ) -> None:
        self.status_code = status_code
        self.ok = status_code < 400
        self.headers = {"content-type": content_type} if content_type else {}
        self.text = text
        self.url = url


def _patch_get(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[dict]:
    calls: list[dict] = []

    def fake_get(url: str, **kwargs: Any) -> Any:
        calls.append({"url": url, **kwargs})
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(web_connector.requests, "get", fake_get)
    return calls


@pytest.mark.parametrize("status", [403, 503])
def test_detects_cloudflare_challenge(status: int) -> None:
    assert is_cloudflare_challenge(status, {"cf-mitigated": "challenge"})


def test_challenge_header_value_is_case_insensitive() -> None:
    assert is_cloudflare_challenge(403, {"cf-mitigated": "Challenge"})


@pytest.mark.parametrize(
    "status,headers",
    [
        (403, {}),  # plain 403 — a real "forbidden", not a bot check
        (403, {"server": "cloudflare"}),  # Cloudflare-fronted but not mitigated
        (200, {"cf-mitigated": "challenge"}),
        (404, {"cf-mitigated": "challenge"}),
        (None, {"cf-mitigated": "challenge"}),
    ],
)
def test_ignores_non_challenge_responses(
    status: int | None, headers: dict[str, str]
) -> None:
    assert not is_cloudflare_challenge(status, headers)


def test_fallback_returns_final_url_and_html(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_get(
        monkeypatch,
        _FakeResponse(url="https://www.w3.org/TR/WCAG22/Overview.html"),
    )
    result = fetch_html_without_browser("https://www.w3.org/TR/WCAG22/")
    assert result == (
        "https://www.w3.org/TR/WCAG22/Overview.html",
        "<html><title>Real page</title></html>",
    )
    # Must not send the browser user agent — that's what Cloudflare challenges.
    assert "headers" not in calls[0]
    assert calls[0]["timeout"] > 0


@pytest.mark.parametrize(
    "response",
    [
        _FakeResponse(status_code=403),  # still challenged / blocked
        _FakeResponse(status_code=500),
        _FakeResponse(content_type="application/pdf"),
        _FakeResponse(content_type=""),
    ],
)
def test_fallback_rejects_unusable_responses(
    monkeypatch: pytest.MonkeyPatch, response: _FakeResponse
) -> None:
    _patch_get(monkeypatch, response)
    assert fetch_html_without_browser("https://www.w3.org/TR/WCAG22/") is None


def test_fallback_swallows_request_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, requests.ConnectionError("boom"))
    assert fetch_html_without_browser("https://www.w3.org/TR/WCAG22/") is None
