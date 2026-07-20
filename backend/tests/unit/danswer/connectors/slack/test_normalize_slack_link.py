import pytest
from slack_sdk.errors import SlackApiError

from danswer.connectors.slack.utils import normalize_slack_link
from danswer.connectors.slack.utils import resolve_workspace_subdomain


class _FakeResp(dict):
    """Minimal stand-in for slack_sdk's SlackResponse (dict-like + validate())."""

    def validate(self) -> "_FakeResp":
        return self


class _FakeClient:
    def __init__(self, permalink: str | None = None, error: str | None = None):
        self._permalink = permalink
        self._error = error

    def chat_getPermalink(self, channel: str, message_ts: str) -> _FakeResp:
        if self._error is not None:
            raise SlackApiError(self._error, {"ok": False, "error": self._error})
        return _FakeResp({"ok": True, "permalink": self._permalink})


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The bug: workspace display name "Product" -> dead product.slack.com.
        (
            "https://Product.slack.com/archives/C02EF2C5KS8/p1712595481616829",
            "https://uipath-product.slack.com/archives/C02EF2C5KS8/p1712595481616829",
        ),
        # Trailing space variant seen in the data ("Product ").
        (
            "https://Product .slack.com/archives/C02/p1?thread_ts=1712595481.616829",
            "https://uipath-product.slack.com/archives/C02/p1?thread_ts=1712595481.616829",
        ),
        # Case-insensitive match.
        (
            "https://product.slack.com/archives/C02/p1",
            "https://uipath-product.slack.com/archives/C02/p1",
        ),
        # Idempotent: already-canonical link is untouched.
        (
            "https://uipath-product.slack.com/archives/C02/p1",
            "https://uipath-product.slack.com/archives/C02/p1",
        ),
        # Genuinely different workspaces must NOT be rewritten.
        (
            "https://uipath-customer-ops.slack.com/archives/C99/p2",
            "https://uipath-customer-ops.slack.com/archives/C99/p2",
        ),
        (
            "https://uipath-marketing.slack.com/archives/C99/p2",
            "https://uipath-marketing.slack.com/archives/C99/p2",
        ),
        # Non-Slack links pass through unchanged (even if a path says "product").
        (
            "https://docs.uipath.com/product/latest",
            "https://docs.uipath.com/product/latest",
        ),
        # Only the host is rewritten, not a matching path segment.
        (
            "https://Product.slack.com/archives/product/p1",
            "https://uipath-product.slack.com/archives/product/p1",
        ),
    ],
)
def test_normalize_slack_link(raw: str, expected: str) -> None:
    assert normalize_slack_link(raw) == expected


def test_normalize_slack_link_empty() -> None:
    assert normalize_slack_link("") == ""


def test_resolve_workspace_subdomain_from_permalink() -> None:
    client = _FakeClient(
        permalink="https://uipath-product.slack.com/archives/C02/p1712595481616829"
    )
    assert (
        resolve_workspace_subdomain(client, "C02", "1712595481.616829", fallback="X")  # type: ignore[arg-type]
        == "uipath-product"
    )


def test_resolve_workspace_subdomain_other_workspace() -> None:
    client = _FakeClient(
        permalink="https://uipath-customer-ops.slack.com/archives/C99/p1"
    )
    assert (
        resolve_workspace_subdomain(client, "C99", "1.1", fallback="X")  # type: ignore[arg-type]
        == "uipath-customer-ops"
    )


@pytest.mark.parametrize(
    "client",
    [
        _FakeClient(error="channel_not_found"),  # API error -> fallback
        _FakeClient(permalink=""),  # empty permalink -> fallback
        _FakeClient(permalink="https://example.com/not-slack"),  # non-slack -> fallback
    ],
)
def test_resolve_workspace_subdomain_falls_back(client: _FakeClient) -> None:
    assert (
        resolve_workspace_subdomain(client, "C1", "1.1", fallback="uipath-product")  # type: ignore[arg-type]
        == "uipath-product"
    )
