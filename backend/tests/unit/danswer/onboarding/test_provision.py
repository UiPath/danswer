"""Unit tests for the onboarding provisioning builders (pure logic — no DB/IO)."""
from danswer.configs.constants import DocumentSource
from danswer.onboarding.provision import _build_channel_config
from danswer.onboarding.provision import _build_connector_base
from danswer.onboarding.provision import _parse_github_repo
from danswer.onboarding.provision import _source_type_value


def test_web_docs_uipath_enables_all_versions() -> None:
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
    assert cfg["web_connector_type"] == "recursive"


def test_web_non_docs_does_not_enable_all_versions() -> None:
    cb = _build_connector_base(
        {"type": "web", "value": "https://example.com/help"},
        db_session=None,  # type: ignore[arg-type]
    )
    assert "uipath_latest_versions" not in cb.connector_specific_config


def test_confluence_source_config() -> None:
    url = "https://uipath.atlassian.net/wiki/spaces/DEV/overview"
    cb = _build_connector_base(
        {"type": "confluence", "value": url}, db_session=None  # type: ignore[arg-type]
    )
    assert cb.source == DocumentSource.CONFLUENCE
    assert cb.connector_specific_config["wiki_page_url"] == url


def test_github_source_config_parses_owner_repo() -> None:
    cb = _build_connector_base(
        {"type": "github", "value": "https://github.com/UiPath/testsfagents"},
        db_session=None,  # type: ignore[arg-type]
    )
    assert cb.source == DocumentSource.GITHUB
    assert cb.connector_specific_config["repo_owner"] == "UiPath"
    assert cb.connector_specific_config["repo_name"] == "testsfagents"


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
