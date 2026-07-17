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
    # Submitted notification links to the request's form so the admin can open,
    # review and approve it directly.
    assert "/admin/onboarding/7" in text


def test_notify_complete_includes_status_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class _Client:
        def __init__(self, token: str) -> None:
            pass

        def chat_postMessage(self, **kwargs: object) -> None:
            captured.update(kwargs)

    _patch_client(monkeypatch, _Client)
    monkeypatch.setattr(notify, "ONBOARDING_NOTIFY_CHANNEL", "darwin-devs")
    monkeypatch.setattr(notify, "WEB_DOMAIN", "https://darwin.example.com")
    notify.notify_onboarding_complete(_req())  # type: ignore[arg-type]

    text = captured["text"]
    assert "is now live in #help-automation-suite" in text
    assert "https://darwin.example.com/admin/onboarding" in text


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


# --- customer-facing thread ------------------------------------------------


def test_client_submitted_returns_ts_mentions_and_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class _Client:
        def __init__(self, token: str) -> None:
            pass

        def conversations_join(self, **kwargs: object) -> None:
            captured["joined"] = kwargs.get("channel")

        def users_lookupByEmail(self, email: str) -> dict:
            return {"user": {"id": "U999"}}

        def chat_postMessage(self, **kwargs: object) -> dict:
            captured.update(kwargs)
            return {"ts": "111.222"}

    _patch_client(monkeypatch, _Client)
    monkeypatch.setattr(notify, "ONBOARDING_CLIENT_CHANNEL", "help-darwin")
    monkeypatch.setattr(notify, "WEB_DOMAIN", "https://darwin.example.com")

    ts = notify.notify_client_submitted(_req())  # type: ignore[arg-type]
    assert ts == "111.222"
    assert captured["channel"] == "help-darwin"
    assert "<@U999>" in captured["text"]  # requester @mentioned
    assert "https://darwin.example.com/onboarding?view=requests" in captured["text"]
    assert "thread_ts" not in captured  # root message, not a reply


def test_client_submitted_mention_falls_back_to_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class _Client:
        def __init__(self, token: str) -> None:
            pass

        def conversations_join(self, **kwargs: object) -> None:
            pass

        def users_lookupByEmail(self, email: str) -> dict:
            raise Exception("users_not_found")

        def chat_postMessage(self, **kwargs: object) -> dict:
            captured.update(kwargs)
            return {"ts": "1"}

    _patch_client(monkeypatch, _Client)
    monkeypatch.setattr(notify, "ONBOARDING_CLIENT_CHANNEL", "help-darwin")
    notify.notify_client_submitted(_req())  # type: ignore[arg-type]
    assert "jo@uipath.com" in captured["text"]  # plain-text fallback


def test_client_reply_skips_without_thread_ts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No stored thread ts -> must NOT post anything (never a top-level message).
    built = {"n": 0}

    class _Client:
        def __init__(self, token: str) -> None:
            built["n"] += 1

        def chat_postMessage(self, **kwargs: object) -> dict:
            built["n"] += 100
            return {"ts": "x"}

    _patch_client(monkeypatch, _Client)
    monkeypatch.setattr(notify, "ONBOARDING_CLIENT_CHANNEL", "help-darwin")
    notify.notify_client_complete(_req())  # type: ignore[arg-type]
    assert built["n"] == 0


def test_client_reply_threads_when_ts_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class _Client:
        def __init__(self, token: str) -> None:
            pass

        def chat_postMessage(self, **kwargs: object) -> dict:
            captured.update(kwargs)
            return {"ts": "y"}

    _patch_client(monkeypatch, _Client)
    monkeypatch.setattr(notify, "ONBOARDING_CLIENT_CHANNEL", "help-darwin")
    req = _req()
    req.help_thread_ts = "111.222"
    notify.notify_client_complete(req)  # type: ignore[arg-type]
    assert captured["thread_ts"] == "111.222"  # posted as a reply
    assert "live in #help-automation-suite" in captured["text"]


def test_resolve_channel_accepts_id_name_or_link() -> None:
    assert notify._resolve_channel("C07B2V8E99S") == "C07B2V8E99S"
    assert notify._resolve_channel("darwin-devs") == "darwin-devs"
    assert (
        notify._resolve_channel(
            "https://uipath.enterprise.slack.com/archives/C07B2V8E99S"
        )
        == "C07B2V8E99S"
    )
    assert notify._resolve_channel("  #ops  ") == "#ops"
