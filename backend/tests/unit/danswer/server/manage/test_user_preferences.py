"""Unit tests for opt-out assistant visibility serialization.

`UserInfo.from_model` is what the frontend reads to learn which assistants a
user has hidden. The opt-out model lives in `hidden_assistants`; these tests pin
its serialization (including the None -> [] normalization) without a DB.
"""
from types import SimpleNamespace

from danswer.auth.schemas import UserRole
from danswer.server.manage.models import UserInfo
from danswer.server.manage.models import UserPreferences


def _fake_user(
    chosen_assistants: list[int] | None,
    hidden_assistants: list[int] | None,
) -> SimpleNamespace:
    # from_model only reads these attributes off the ORM user.
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        email="user@example.com",
        is_active=True,
        is_superuser=False,
        is_verified=True,
        role=UserRole.BASIC,
        chosen_assistants=chosen_assistants,
        hidden_assistants=hidden_assistants,
    )


def test_from_model_exposes_hidden_assistants() -> None:
    info = UserInfo.from_model(_fake_user(chosen_assistants=[2, 1], hidden_assistants=[3]))
    assert info.preferences.hidden_assistants == [3]
    assert info.preferences.chosen_assistants == [2, 1]


def test_from_model_normalizes_null_hidden_to_empty_list() -> None:
    # Existing rows / no-preference users must serialize as "nothing hidden"
    # (i.e. everything visible) rather than null.
    info = UserInfo.from_model(_fake_user(chosen_assistants=None, hidden_assistants=None))
    assert info.preferences.hidden_assistants == []


def test_user_preferences_hidden_defaults_to_none() -> None:
    # The field is optional on the wire so older clients/payloads still parse.
    prefs = UserPreferences(chosen_assistants=None)
    assert prefs.hidden_assistants is None
