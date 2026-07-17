"""Unit tests for the onboarding provisioning builders (pure logic — no DB/IO)."""
import pytest

from danswer.configs.constants import DocumentSource
from danswer.onboarding import provision
from danswer.onboarding.provision import _build_channel_config
from danswer.onboarding.provision import _build_connector_base
from danswer.onboarding.provision import _parse_github_repo
from danswer.onboarding.provision import _source_type_value


def _allow_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the web SSRF guard see any host as a public address, offline."""
    monkeypatch.setattr(
        provision.socket,
        "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )


def test_web_docs_uipath_enables_all_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_public_dns(monkeypatch)
    cb = _build_connector_base(
        {
            "type": "web",
            "value": "https://docs.uipath.com/orchestrator/automation-cloud/latest",
        },
        db_session=None,  # type: ignore[arg-type]  # web path doesn't touch the db
    )
    assert cb.source == DocumentSource.WEB
    cfg = cb.connector_specific_config
    assert cfg["base_url"].startswith("https://docs.uipath.com/orchestrator")
    assert cfg["uipath_latest_versions"] is True
    assert cfg["max_versions"] == 3  # latest 3 versions
    assert cfg["web_connector_type"] == "recursive"


def test_web_non_docs_does_not_enable_all_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_public_dns(monkeypatch)
    cb = _build_connector_base(
        {"type": "web", "value": "https://example.com/help"},
        db_session=None,  # type: ignore[arg-type]
    )
    assert "uipath_latest_versions" not in cb.connector_specific_config


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/internal",  # loopback
        "http://10.0.0.5/admin",  # RFC1918 private
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "file:///etc/passwd",  # non-http scheme
    ],
)
def test_web_rejects_internal_targets(url: str) -> None:
    # IP-literal / bad-scheme rejects need no DNS, so no monkeypatch required.
    with pytest.raises(ValueError):
        _build_connector_base(
            {"type": "web", "value": url}, db_session=None  # type: ignore[arg-type]
        )


def test_confluence_source_config(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://uipath.atlassian.net/wiki/spaces/DEV/overview"
    monkeypatch.setattr(
        provision, "_allowed_confluence_hosts", lambda db: {"uipath.atlassian.net"}
    )
    cb = _build_connector_base(
        {"type": "confluence", "value": url}, db_session=None  # type: ignore[arg-type]
    )
    assert cb.source == DocumentSource.CONFLUENCE
    assert cb.connector_specific_config["wiki_page_url"] == url


def test_confluence_rejects_unknown_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provision, "_allowed_confluence_hosts", lambda db: {"uipath.atlassian.net"}
    )
    with pytest.raises(ValueError):
        _build_connector_base(
            {"type": "confluence", "value": "https://evil.example.com/wiki/spaces/X"},
            db_session=None,  # type: ignore[arg-type]
        )


def test_confluence_rejects_when_no_known_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No existing Confluence connector to vet against -> fail closed.
    monkeypatch.setattr(provision, "_allowed_confluence_hosts", lambda db: set())
    with pytest.raises(ValueError):
        _build_connector_base(
            {
                "type": "confluence",
                "value": "https://uipath.atlassian.net/wiki/spaces/X",
            },
            db_session=None,  # type: ignore[arg-type]
        )


def test_github_source_config_parses_owner_repo() -> None:
    cb = _build_connector_base(
        {"type": "github", "value": "https://github.com/UiPath/testsfagents"},
        db_session=None,  # type: ignore[arg-type]
    )
    assert cb.source == DocumentSource.GITHUB
    assert cb.connector_specific_config["repo_owner"] == "UiPath"
    assert cb.connector_specific_config["repo_name"] == "testsfagents"


def test_github_rejects_non_github_host() -> None:
    with pytest.raises(ValueError):
        _build_connector_base(
            {"type": "github", "value": "https://evil.example.com/UiPath/x"},
            db_session=None,  # type: ignore[arg-type]
        )


# --- provision-time credential-access preflight ---------------------------

from danswer.onboarding.provision import _assert_source_accessible  # noqa: E402
from danswer.onboarding.validation import ValidationResult  # noqa: E402


def test_preflight_web_is_noop() -> None:
    # Web is public (no credential) — preflight must not raise.
    _assert_source_accessible(
        {"type": "web", "value": "https://example.com"}, db_session=None  # type: ignore[arg-type]
    )


def test_preflight_raises_when_inaccessible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provision,
        "validate_github_repo",
        lambda value, db: ValidationResult(valid=False, message="no access"),
    )
    with pytest.raises(ValueError, match="not accessible"):
        _assert_source_accessible(
            {"type": "github", "value": "https://github.com/UiPath/x"},
            db_session=None,  # type: ignore[arg-type]
        )


def test_preflight_passes_when_accessible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provision,
        "validate_slack_channel",
        lambda value: ValidationResult(valid=True, message="#ok"),
    )
    _assert_source_accessible(
        {"type": "slack", "value": "help-x"}, db_session=None  # type: ignore[arg-type]
    )


def test_parse_github_repo() -> None:
    assert _parse_github_repo("https://github.com/UiPath/danswer") == (
        "UiPath",
        "danswer",
    )


def test_source_type_value_mapping() -> None:
    assert _source_type_value("web") == DocumentSource.WEB.value
    assert _source_type_value("confluence") == DocumentSource.CONFLUENCE.value
    assert _source_type_value("github") == DocumentSource.GITHUB.value
    assert _source_type_value("slack") == DocumentSource.SLACK.value


def _payload(**over: object) -> dict:
    base = {
        "team_name": "Team X",
        "channel": {"channel_id": "C1", "channel_name": "help-x"},
        "respond_tag_only": True,
        "sme": {"enabled": False, "group_name": ""},
        "oncall": {"enabled": False, "schedule": ""},
        "jira": {"enabled": False},
    }
    base.update(over)
    return base


def test_channel_config_basic() -> None:
    cfg = _build_channel_config(_payload(), prioritized_sources=["confluence", "web"])
    assert cfg["channel_names"] == ["help-x"]
    assert cfg["respond_tag_only"] is True
    assert cfg["prioritized_sources"] == ["confluence", "web"]
    assert "enable_sme_validation" not in cfg
    assert "opsgenie_schedule" not in cfg
    assert "jira_config" not in cfg


def test_channel_config_all_options() -> None:
    cfg = _build_channel_config(
        _payload(
            sme={"enabled": True, "group_name": "as-smes"},
            oncall={"enabled": True, "schedule": "AS-OnCall"},
            jira={
                "enabled": True,
                "project_key": "AS",
                "issue_type": "Bug",
                "component": "core",
            },
        ),
        prioritized_sources=["web"],
    )
    assert cfg["enable_sme_validation"] is True
    assert cfg["sme_group_name"] == "as-smes"
    assert cfg["opsgenie_schedule"] == "AS-OnCall"
    assert cfg["jira_config"]["enable_jira_integration"] is True
    assert cfg["jira_config"]["project_key"] == "AS"


def test_channel_config_oncall_enabled_but_no_schedule_omitted() -> None:
    # Enabling on-call without a schedule shouldn't write an empty opsgenie value.
    cfg = _build_channel_config(
        _payload(oncall={"enabled": True, "schedule": ""}),
        prioritized_sources=[],
    )
    assert "opsgenie_schedule" not in cfg


# --- per-source refresh cadence -------------------------------------------

from danswer.onboarding.provision import _MONTHLY_REFRESH_FREQ  # noqa: E402
from danswer.onboarding.provision import DEFAULT_REFRESH_FREQ  # noqa: E402
from danswer.onboarding.provision import _dedup_confluence_sources  # noqa: E402
from danswer.onboarding.provision import _prioritized_sources  # noqa: E402


def test_web_source_refresh_is_monthly(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_public_dns(monkeypatch)
    cb = _build_connector_base(
        {"type": "web", "value": "https://docs.uipath.com/x/latest"},
        db_session=None,  # type: ignore[arg-type]
    )
    assert cb.refresh_freq == _MONTHLY_REFRESH_FREQ


def test_confluence_source_refresh_is_daily(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provision, "_allowed_confluence_hosts", lambda db: {"x.atlassian.net"}
    )
    cb = _build_connector_base(
        {"type": "confluence", "value": "https://x.atlassian.net/wiki/spaces/DEV/x"},
        db_session=None,  # type: ignore[arg-type]
    )
    assert cb.refresh_freq == DEFAULT_REFRESH_FREQ


# --- prioritized sources ---------------------------------------------------


def test_prioritized_sources_distinct_in_order() -> None:
    sources = [
        {"type": "web", "value": "a"},
        {"type": "slack", "value": "b"},
        {"type": "web", "value": "c"},  # duplicate type
    ]
    assert _prioritized_sources(sources) == ["web", "slack"]


# --- confluence parent/child dedup ----------------------------------------


def test_dedup_passthrough_when_fewer_than_two_confluence() -> None:
    sources = [
        {"type": "confluence", "value": "https://x.atlassian.net/wiki/spaces/DEV/x"},
        {"type": "web", "value": "https://docs.example.com"},
    ]
    assert _dedup_confluence_sources(sources, db_session=None) == sources  # type: ignore[arg-type]


def test_dedup_space_supersedes_page() -> None:
    space = {
        "type": "confluence",
        "value": "https://x.atlassian.net/wiki/spaces/DEV/overview",
    }
    page = {
        "type": "confluence",
        "value": "https://x.atlassian.net/wiki/spaces/DEV/pages/123/Title",
    }
    out = _dedup_confluence_sources([space, page], db_session=None)  # type: ignore[arg-type]
    assert out == [space]  # the page (child of the space) is dropped


def test_dedup_parent_page_supersedes_child(monkeypatch: pytest.MonkeyPatch) -> None:
    parent = {
        "type": "confluence",
        "value": "https://x.atlassian.net/wiki/spaces/DEV/pages/100/Parent",
    }
    child = {
        "type": "confluence",
        "value": "https://x.atlassian.net/wiki/spaces/DEV/pages/200/Child",
    }
    # 200's ancestor is 100 (also provided) -> child dropped.
    monkeypatch.setattr(
        provision,
        "confluence_page_ancestors",
        lambda ids, db: {"100": [], "200": ["100"]},
    )
    out = _dedup_confluence_sources([parent, child], db_session=None)  # type: ignore[arg-type]
    assert out == [parent]


def test_dedup_keeps_unrelated_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    a = {
        "type": "confluence",
        "value": "https://x.atlassian.net/wiki/spaces/DEV/pages/100/A",
    }
    b = {
        "type": "confluence",
        "value": "https://x.atlassian.net/wiki/spaces/DEV/pages/300/B",
    }
    monkeypatch.setattr(
        provision, "confluence_page_ancestors", lambda ids, db: {"100": [], "300": []}
    )
    out = _dedup_confluence_sources([a, b], db_session=None)  # type: ignore[arg-type]
    assert out == [a, b]


# --- provision_onboarding: cc_pair_ids records real cc_pair ids ------------

from types import SimpleNamespace  # noqa: E402

from danswer.db.models import OnboardingStatus  # noqa: E402
from danswer.onboarding.provision import provision_onboarding  # noqa: E402


def test_provision_records_cc_pair_ids_not_connector_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: in this fork add_credential_to_connector returns
    data=connector_id (not the cc_pair id). provision_onboarding must store the
    real cc_pair ids on the request — otherwise the finalizer/status track the
    wrong pairs (the bug that surfaced an unrelated '[local-copy] jira' and
    dropped a real source). We simulate connector_id != cc_pair_id and assert the
    request gets the cc_pair ids."""
    # Each created connector gets an incrementing id (1, 2, ...).
    connector_ids = iter(range(1, 100))
    captured: dict = {}

    monkeypatch.setattr(provision, "update_onboarding_status", lambda *a, **k: None)
    monkeypatch.setattr(
        provision, "get_current_db_embedding_model", lambda db: SimpleNamespace(id=1)
    )
    monkeypatch.setattr(
        provision, "_dedup_confluence_sources", lambda sources, db: sources
    )
    monkeypatch.setattr(provision, "_assert_source_accessible", lambda s, db: None)
    monkeypatch.setattr(provision, "_build_connector_base", lambda s, db: s)
    monkeypatch.setattr(
        provision,
        "create_connector",
        lambda base, db: SimpleNamespace(id=next(connector_ids)),
    )
    monkeypatch.setattr(provision, "_credential_id_for_source", lambda stype, db: 500)
    # The fork's real return shape: .data is the CONNECTOR id.
    monkeypatch.setattr(
        provision,
        "add_credential_to_connector",
        lambda **kw: SimpleNamespace(data=kw["connector_id"]),
    )
    # The real cc_pair id is deliberately different (connector_id + 1000).
    monkeypatch.setattr(
        provision,
        "get_connector_credential_pair",
        lambda connector_id, credential_id, db: SimpleNamespace(id=connector_id + 1000),
    )
    monkeypatch.setattr(
        provision,
        "set_provisioned_ids",
        lambda db, req, cc_pair_ids: captured.__setitem__("cc_pair_ids", cc_pair_ids),
    )
    monkeypatch.setattr(provision, "create_index_attempt", lambda **k: 0)

    request = SimpleNamespace(
        id=1,
        payload={
            "team_name": "T",
            "sources": [
                {"type": "web", "value": "https://a", "label": "A"},
                {"type": "slack", "value": "chan", "label": "B"},
            ],
        },
    )
    provision_onboarding(request, SimpleNamespace(id=42), None)  # type: ignore[arg-type]

    # connectors were 1 and 2; real cc_pair ids are 1001 and 1002 — NOT [1, 2].
    assert captured["cc_pair_ids"] == [1001, 1002]


def test_provision_marks_failed_when_cc_pair_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the freshly-created pair can't be found, provisioning fails loudly
    (marks FAILED + raises) rather than silently recording a bad id."""
    statuses: list = []
    monkeypatch.setattr(
        provision,
        "update_onboarding_status",
        lambda db, req, status, **kw: statuses.append(status),
    )
    monkeypatch.setattr(
        provision, "get_current_db_embedding_model", lambda db: SimpleNamespace(id=1)
    )
    monkeypatch.setattr(
        provision, "_dedup_confluence_sources", lambda sources, db: sources
    )
    monkeypatch.setattr(provision, "_assert_source_accessible", lambda s, db: None)
    monkeypatch.setattr(provision, "_build_connector_base", lambda s, db: s)
    monkeypatch.setattr(
        provision, "create_connector", lambda base, db: SimpleNamespace(id=7)
    )
    monkeypatch.setattr(provision, "_credential_id_for_source", lambda stype, db: 500)
    monkeypatch.setattr(
        provision, "add_credential_to_connector", lambda **kw: SimpleNamespace(data=7)
    )
    monkeypatch.setattr(
        provision, "get_connector_credential_pair", lambda c, cr, db: None
    )
    monkeypatch.setattr(provision, "set_provisioned_ids", lambda *a, **k: None)

    request = SimpleNamespace(
        id=1,
        payload={
            "team_name": "T",
            "sources": [{"type": "web", "value": "https://a", "label": "A"}],
        },
    )
    with pytest.raises(ValueError):
        provision_onboarding(request, SimpleNamespace(id=42), None)  # type: ignore[arg-type]
    assert OnboardingStatus.FAILED in statuses
