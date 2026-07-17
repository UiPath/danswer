"""Unit tests for onboarding docs-URL validation (pure — no network).

Slack/Confluence validators hit live APIs and are covered by manual/integration
checks, not here."""
from danswer.onboarding.validation import validate_docs_url


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
