"""Unit tests for onboarding docs-URL validation (pure — no network).

Slack/Confluence validators hit live APIs and are covered by manual/integration
checks, not here."""
import pytest

from danswer.onboarding.validation import validate_docs_url
from danswer.onboarding.validation import validate_github_repo


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
