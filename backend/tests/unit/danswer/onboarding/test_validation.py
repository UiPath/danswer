"""Unit tests for onboarding docs-URL validation (pure — no network).

Slack/Confluence validators hit live APIs and are covered by manual/integration
checks, not here."""
import pytest

from danswer.onboarding import validation
from danswer.onboarding.validation import validate_docs_url
from danswer.onboarding.validation import validate_github_repo
from danswer.onboarding.validation import validate_jira_filter


class _Cred:
    def __init__(self, cj: dict) -> None:
        self.credential_json = cj


class _Scalars:
    def __init__(self, creds: list) -> None:
        self._creds = creds

    def scalars(self):  # type: ignore[no-untyped-def]
        return iter(self._creds)


class _FakeDb:
    """Minimal Session stand-in for `_first_github_token`'s single query."""

    def __init__(self, creds: list) -> None:
        self._creds = creds

    def execute(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return _Scalars(self._creds)


def test_docs_url_valid_root_normalizes() -> None:
    r = validate_docs_url(
        "https://docs.uipath.com/orchestrator/automation-cloud/latest/user-guide/x"
    )
    assert r.valid
    # Normalized to the product root (version/'latest' segment stripped).
    assert (
        r.resolved["root_url"]
        == "https://docs.uipath.com/orchestrator/automation-cloud"
    )


def test_docs_url_rejects_non_docs_domain() -> None:
    r = validate_docs_url("https://example.com/foo")
    assert not r.valid


def test_docs_url_requires_product_path() -> None:
    r = validate_docs_url("https://docs.uipath.com/")
    assert not r.valid


def test_docs_url_empty() -> None:
    assert not validate_docs_url("").valid


# --- validate_github_repo -------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "",  # empty
        "https://gitlab.com/UiPath/x",  # wrong host
        "https://evil.example.com/UiPath/x",  # non-github host
        "https://github.com/UiPath",  # missing repo segment
    ],
)
def test_github_repo_rejects_bad_input(url: str) -> None:
    # These all return before touching the DB, so db_session can be None.
    assert not validate_github_repo(url, db_session=None).valid  # type: ignore[arg-type]


def test_github_repo_no_credential_is_unverified() -> None:
    # No GitHub token on file -> valid but explicitly "unverified".
    r = validate_github_repo("https://github.com/UiPath/danswer", _FakeDb([]))  # type: ignore[arg-type]
    assert r.valid
    assert "unverified" in r.message.lower()
    assert r.resolved == {"repo_owner": "UiPath", "repo_name": "danswer"}


def test_github_repo_accessible(monkeypatch: pytest.MonkeyPatch) -> None:
    import github as github_mod

    class _OkGithub:
        def __init__(self, token: str, base_url: str | None = None) -> None:
            pass

        def get_repo(self, full_name: str) -> object:
            return object()

    monkeypatch.setattr(github_mod, "Github", _OkGithub)
    db = _FakeDb([_Cred({"github_access_token": "ghp_fake"})])
    r = validate_github_repo("https://github.com/UiPath/danswer.git", db)  # type: ignore[arg-type]
    assert r.valid
    assert r.message == "UiPath/danswer"  # .git suffix stripped


def test_github_repo_inaccessible_reports_generic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import github as github_mod

    class _DenyGithub:
        def __init__(self, token: str, base_url: str | None = None) -> None:
            pass

        def get_repo(self, full_name: str) -> object:
            # Message intentionally contains a token-like string to prove it is
            # NOT propagated into the ValidationResult.
            raise Exception("404 {'token': 'ghp_should_not_leak'}")

    monkeypatch.setattr(github_mod, "Github", _DenyGithub)
    db = _FakeDb([_Cred({"github_access_token": "ghp_fake"})])
    r = validate_github_repo("https://github.com/UiPath/secret-repo", db)  # type: ignore[arg-type]
    assert not r.valid
    assert "not found or the GitHub app lacks access" in r.message
    assert "ghp_" not in r.message  # no token / raw payload leakage


# --- validate_jira_filter -------------------------------------------------


def test_jira_filter_empty() -> None:
    assert not validate_jira_filter("", db_session=None).valid  # type: ignore[arg-type]


def test_jira_filter_unverified_without_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validation, "jira_base_url", lambda db: None)
    monkeypatch.setattr(validation, "_first_jira_credential", lambda db: None)
    r = validate_jira_filter("project = ABC", db_session=None)  # type: ignore[arg-type]
    assert r.valid
    assert "unverified" in r.message.lower()


def test_jira_filter_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    import jira as jira_mod

    class _OkJira:
        def __init__(self, **kwargs: object) -> None:
            pass

        def search_issues(self, jql: str, maxResults: int = 1) -> list:
            return []

    monkeypatch.setattr(
        validation, "jira_base_url", lambda db: "https://x.atlassian.net"
    )
    monkeypatch.setattr(
        validation,
        "_first_jira_credential",
        lambda db: {"jira_api_token": "t", "jira_user_email": "e@x.com"},
    )
    monkeypatch.setattr(jira_mod, "JIRA", _OkJira)
    r = validate_jira_filter("project = ABC", db_session=None)  # type: ignore[arg-type]
    assert r.valid
    assert r.message == "Jira filter OK"


def test_jira_filter_invalid_reports_generic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jira as jira_mod

    class _BadJira:
        def __init__(self, **kwargs: object) -> None:
            pass

        def search_issues(self, jql: str, maxResults: int = 1) -> list:
            raise Exception("400 bad JQL token=ghp_should_not_leak")

    monkeypatch.setattr(
        validation, "jira_base_url", lambda db: "https://x.atlassian.net"
    )
    monkeypatch.setattr(
        validation, "_first_jira_credential", lambda db: {"jira_api_token": "t"}
    )
    monkeypatch.setattr(jira_mod, "JIRA", _BadJira)
    r = validate_jira_filter("nonsense jql", db_session=None)  # type: ignore[arg-type]
    assert not r.valid
    assert "Invalid Jira filter" in r.message
    assert "token=" not in r.message  # no payload/token leakage


# --- validate_slack_channel (mocked bot client) ---------------------------
from slack_sdk.errors import SlackApiError  # noqa: E402

from danswer.onboarding.validation import validate_slack_channel  # noqa: E402
from danswer.onboarding.validation import validate_slack_group  # noqa: E402
from danswer.onboarding.validation import validate_confluence_url  # noqa: E402


class _SlackClient:
    """Fake WebClient covering conversations_info + users_conversations paging."""

    def __init__(
        self,
        info: dict | None = None,
        info_error: str | None = None,
        member_channels: list | None = None,
    ):
        self._info = info
        self._info_error = info_error
        self._member = member_channels or []

    def conversations_info(self, channel: str) -> dict:
        if self._info_error:
            raise SlackApiError(self._info_error, {"error": self._info_error})
        return {"channel": self._info}

    def users_conversations(
        self, limit: int = 1000, cursor: str | None = None, **kw: object
    ) -> dict:
        return {"channels": self._member, "response_metadata": {"next_cursor": ""}}


def _patch_bot(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    monkeypatch.setattr(validation, "_bot_client", lambda: client)


def test_slack_channel_empty() -> None:
    assert not validate_slack_channel("").valid


def test_slack_channel_from_link(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_bot(monkeypatch, _SlackClient(info={"id": "C0ABC123", "name": "help-team"}))
    r = validate_slack_channel(
        "https://uipath-product.slack.com/archives/C0ABC123/p1712"
    )
    assert r.valid
    assert r.resolved["channel_id"] == "C0ABC123"
    assert r.resolved["channel_name"] == "help-team"


def test_slack_channel_from_mention(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_bot(monkeypatch, _SlackClient(info={"id": "C0ABC123", "name": "help-team"}))
    assert validate_slack_channel("<#C0ABC123|help-team>").valid


def test_slack_channel_link_member(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_bot(
        monkeypatch,
        _SlackClient(info={"id": "C1", "name": "help-team", "is_member": True}),
    )
    r = validate_slack_channel("https://x.slack.com/archives/C1")
    assert r.valid
    assert "already in this channel" in r.message


def test_slack_channel_link_public_not_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bot(
        monkeypatch,
        _SlackClient(
            info={
                "id": "C1",
                "name": "help-team",
                "is_member": False,
                "is_private": False,
            }
        ),
    )
    r = validate_slack_channel("https://x.slack.com/archives/C1")
    assert r.valid
    assert "auto-join" in r.message  # public -> connector self-joins


def test_slack_channel_link_private_not_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bot(
        monkeypatch,
        _SlackClient(
            info={
                "id": "C1",
                "name": "secret",
                "is_member": False,
                "is_private": True,
            }
        ),
    )
    r = validate_slack_channel("https://x.slack.com/archives/C1")
    assert r.valid
    assert "invite" in r.message.lower()
    assert "private" in r.message.lower()  # private -> must be invited


def test_slack_channel_bad_id_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_bot(monkeypatch, _SlackClient(info_error="channel_not_found"))
    r = validate_slack_channel("C0DEADBEEF")
    assert not r.valid
    assert "not found" in r.message.lower()


def test_slack_channel_bare_name_member(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_bot(
        monkeypatch,
        _SlackClient(member_channels=[{"id": "C1", "name": "help-team"}]),
    )
    r = validate_slack_channel("help-team")
    assert r.valid
    assert "already in this channel" in r.message
    assert r.resolved["channel_id"] == "C1"


def test_slack_channel_bare_name_not_member_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not in the bot's channels yet -> still accepted (bot added during onboarding).
    _patch_bot(monkeypatch, _SlackClient(member_channels=[]))
    r = validate_slack_channel("brand-new-channel")
    assert r.valid
    assert "auto-join" in r.message  # public auto-join; private needs an invite
    assert r.resolved["channel_name"] == "brand-new-channel"


# --- validate_slack_group (comma-separated handles) -----------------------


def test_slack_group_empty() -> None:
    assert not validate_slack_group("").valid


def test_slack_group_all_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_bot(monkeypatch, object())
    monkeypatch.setattr(
        validation,
        "fetch_groupids_from_names",
        lambda handles, client: (["G1", "G2"], []),
    )
    r = validate_slack_group("@as-dri, @sre-dri")
    assert r.valid
    assert r.message == "@as-dri, @sre-dri"


def test_slack_group_some_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_bot(monkeypatch, object())
    monkeypatch.setattr(
        validation,
        "fetch_groupids_from_names",
        lambda handles, client: (["G1"], ["sre-dri"]),
    )
    r = validate_slack_group("@as-dri, @sre-dri")
    assert not r.valid
    assert "sre-dri" in r.message


# --- validate_confluence_url (mocked host allowlist + client) -------------


def test_confluence_url_bad_host_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        validation, "_allowed_confluence_hosts", lambda db: {"uipath.atlassian.net"}
    )
    r = validate_confluence_url("https://evil.atlassian.net/wiki/spaces/X", None)  # type: ignore[arg-type]
    assert not r.valid
    assert "host not allowed" in r.message.lower()


def test_confluence_url_unverified_without_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validation, "_allowed_confluence_hosts", lambda db: set())
    monkeypatch.setattr(validation, "_first_confluence_credential", lambda db: None)
    r = validate_confluence_url("https://uipath.atlassian.net/wiki/spaces/DEV/x", None)  # type: ignore[arg-type]
    assert r.valid
    assert "unverified" in r.message.lower()


def test_confluence_url_space_accessible(monkeypatch: pytest.MonkeyPatch) -> None:
    import atlassian as atlassian_mod

    class _OkConfluence:
        def __init__(self, **kwargs: object) -> None:
            pass

        def get_space(self, space: str) -> dict:
            return {"key": space}

    monkeypatch.setattr(
        validation, "_allowed_confluence_hosts", lambda db: {"uipath.atlassian.net"}
    )
    monkeypatch.setattr(
        validation,
        "_first_confluence_credential",
        lambda db: {"confluence_username": "u", "confluence_access_token": "t"},
    )
    monkeypatch.setattr(atlassian_mod, "Confluence", _OkConfluence)
    r = validate_confluence_url("https://uipath.atlassian.net/wiki/spaces/DEV/x", None)  # type: ignore[arg-type]
    assert r.valid
    assert r.resolved["space"] == "DEV"
    assert "whole" in r.message and "space" in r.message  # space url -> whole space


def test_confluence_url_page_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    import atlassian as atlassian_mod

    class _OkConfluence:
        def __init__(self, **kwargs: object) -> None:
            pass

        def get_page_by_id(self, page_id: str) -> dict:
            return {"id": page_id}

    monkeypatch.setattr(
        validation, "_allowed_confluence_hosts", lambda db: {"uipath.atlassian.net"}
    )
    monkeypatch.setattr(
        validation,
        "_first_confluence_credential",
        lambda db: {"confluence_username": "u", "confluence_access_token": "t"},
    )
    monkeypatch.setattr(atlassian_mod, "Confluence", _OkConfluence)
    r = validate_confluence_url(
        "https://uipath.atlassian.net/wiki/spaces/DEV/pages/88998707616/Pro+Dev",
        None,  # type: ignore[arg-type]
    )
    assert r.valid
    # page url -> page + children, NOT the whole space
    assert "child pages" in r.message
    assert "whole" not in r.message
    assert r.resolved["page_id"] == "88998707616"


def test_confluence_url_page_inaccessible(monkeypatch: pytest.MonkeyPatch) -> None:
    import atlassian as atlassian_mod

    class _DenyConfluence:
        def __init__(self, **kwargs: object) -> None:
            pass

        def get_page_by_id(self, page_id: str) -> dict:
            raise Exception("403 forbidden token=secret")

    monkeypatch.setattr(
        validation, "_allowed_confluence_hosts", lambda db: {"uipath.atlassian.net"}
    )
    monkeypatch.setattr(
        validation,
        "_first_confluence_credential",
        lambda db: {"confluence_username": "u", "confluence_access_token": "t"},
    )
    monkeypatch.setattr(atlassian_mod, "Confluence", _DenyConfluence)
    r = validate_confluence_url(
        "https://uipath.atlassian.net/wiki/spaces/DEV/pages/123/T", None  # type: ignore[arg-type]
    )
    assert not r.valid
    assert "Page not found" in r.message
    assert "secret" not in r.message  # no token/exception leakage


def test_confluence_url_space_inaccessible(monkeypatch: pytest.MonkeyPatch) -> None:
    import atlassian as atlassian_mod

    class _DenyConfluence:
        def __init__(self, **kwargs: object) -> None:
            pass

        def get_space(self, space: str) -> dict:
            raise Exception("403 forbidden token=secret")

    monkeypatch.setattr(
        validation, "_allowed_confluence_hosts", lambda db: {"uipath.atlassian.net"}
    )
    monkeypatch.setattr(
        validation,
        "_first_confluence_credential",
        lambda db: {"confluence_username": "u", "confluence_access_token": "t"},
    )
    monkeypatch.setattr(atlassian_mod, "Confluence", _DenyConfluence)
    r = validate_confluence_url("https://uipath.atlassian.net/wiki/spaces/DEV/x", None)  # type: ignore[arg-type]
    assert not r.valid
    assert "secret" not in r.message  # no token/exception leakage
