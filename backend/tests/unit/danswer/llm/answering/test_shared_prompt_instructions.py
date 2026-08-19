"""Unit tests for with_shared_instructions — the Admin → Settings
`shared_prompt_instructions` injection applied to every assistant's PromptConfig."""
from unittest.mock import patch

from danswer.llm.answering.models import PromptConfig
from danswer.llm.answering.models import with_shared_instructions
from danswer.server.settings.models import Settings


def _pc() -> PromptConfig:
    return PromptConfig(
        system_prompt="You are a helpful assistant.",
        task_prompt="Answer the question.",
        datetime_aware=False,
        include_citations=True,
    )


def _with_setting(value: str) -> PromptConfig:
    with patch(
        "danswer.server.settings.store.load_settings",
        return_value=Settings(shared_prompt_instructions=value),
    ):
        return with_shared_instructions(_pc())


def test_appends_instructions_to_system_prompt() -> None:
    out = _with_setting("Answer evidenced parts; defer only the rest.")
    assert out.system_prompt == (
        "You are a helpful assistant.\n\n"
        "Answer evidenced parts; defer only the rest."
    )
    # everything else untouched
    assert out.task_prompt == "Answer the question."
    assert out.include_citations is True


def test_empty_or_whitespace_setting_is_a_noop() -> None:
    pc = _pc()
    for value in ("", "   \n  "):
        with patch(
            "danswer.server.settings.store.load_settings",
            return_value=Settings(shared_prompt_instructions=value),
        ):
            assert with_shared_instructions(pc) is pc


def test_persona_opt_out_skips_injection() -> None:
    # a persona with shared_instructions_enabled=False must NOT get the injection;
    # True (or a persona without the attr, or None) keeps it.
    from types import SimpleNamespace

    pc = _pc()
    with patch(
        "danswer.server.settings.store.load_settings",
        return_value=Settings(shared_prompt_instructions="POLICY."),
    ):
        opted_out = SimpleNamespace(shared_instructions_enabled=False)
        assert with_shared_instructions(pc, persona=opted_out) is pc

        opted_in = SimpleNamespace(shared_instructions_enabled=True)
        assert "POLICY." in with_shared_instructions(pc, persona=opted_in).system_prompt
        assert "POLICY." in with_shared_instructions(pc).system_prompt  # no persona


def test_settings_store_failure_fails_open() -> None:
    # a settings-store hiccup must never break answering — return unchanged
    pc = _pc()
    with patch(
        "danswer.server.settings.store.load_settings",
        side_effect=RuntimeError("kv store down"),
    ):
        assert with_shared_instructions(pc) is pc
