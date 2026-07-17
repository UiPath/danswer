"""Unit tests for onboarding Slack notifications (mocked — no network)."""
from types import SimpleNamespace

import pytest

from danswer.onboarding import notify


def _req() -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        requester_email="jo@uipath.com",
        payload={
            "team_name": "Automation Suite",
            "channel": {"channel_name": "help-automation-suite"},
            "sources": [{"type": "web", "label": "Docs (cloud)"}],
        },
    )


def _patch_client(monkeypatch: pytest.MonkeyPatch, client_cls: type) -> None:
    monkeypatch.setattr(notify, "WebClient", client_cls)
    monkeypatch.setattr(notify, "fetch_tokens", lambda: SimpleNamespace(bot_token="x"))


def test_notify_posts_message(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class _Client:
        def __init__(self, token: str) -> None:
            pass

        def chat_postMessage(self, **kwargs: object) -> None:
            captured.update(kwargs)

    _patch_client(monkeypatch, _Client)
    monkeypatch.setattr(notify, "ONBOARDING_NOTIFY_CHANNEL", "darwin-devs")
    notify.notify_onboarding_submitted(_req())  # type: ignore[arg-type]

    assert captured["channel"] == "darwin-devs"
    text = captured["text"]
    assert "jo@uipath.com" in text
    assert "Automation Suite" in text
    assert "help-automation-suite" in text


def test_notify_swallows_slack_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client:
        def __init__(self, token: str) -> None:
            pass

        def chat_postMessage(self, **kwargs: object) -> None:
            raise Exception("slack is down")

    _patch_client(monkeypatch, _Client)
    monkeypatch.setattr(notify, "ONBOARDING_NOTIFY_CHANNEL", "darwin-devs")
    # Must not raise — submission must never fail on a notification hiccup.
    notify.notify_onboarding_submitted(_req())  # type: ignore[arg-type]


def test_notify_noop_when_channel_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    created = {"n": 0}

    class _Client:
        def __init__(self, token: str) -> None:
            created["n"] += 1

    _patch_client(monkeypatch, _Client)
    monkeypatch.setattr(notify, "ONBOARDING_NOTIFY_CHANNEL", "")
    notify.notify_onboarding_submitted(_req())  # type: ignore[arg-type]
    assert created["n"] == 0  # no client built, nothing posted
