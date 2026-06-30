"""Unit tests for the auto-search rollout gate (_auto_search_allowed).

This is the security boundary for the admin-only / staged rollout of the
auto-routed Search tab — the /auto-search endpoint relies on it to 403
non-admins, independent of the UI hiding the tab. Parity with the rollout
states matters, so it's locked down here.
"""
from types import SimpleNamespace

from danswer.auth.schemas import UserRole
from danswer.server.query_and_chat.query_backend import _auto_search_allowed
from danswer.server.settings.models import AutoSearchRollout


_ADMIN = SimpleNamespace(role=UserRole.ADMIN)
_BASIC = SimpleNamespace(role=UserRole.BASIC)


def test_off_blocks_everyone() -> None:
    assert _auto_search_allowed(AutoSearchRollout.OFF, _ADMIN) is False
    assert _auto_search_allowed(AutoSearchRollout.OFF, _BASIC) is False
    assert _auto_search_allowed(AutoSearchRollout.OFF, None) is False


def test_everyone_allows_all() -> None:
    assert _auto_search_allowed(AutoSearchRollout.EVERYONE, _ADMIN) is True
    assert _auto_search_allowed(AutoSearchRollout.EVERYONE, _BASIC) is True
    assert _auto_search_allowed(AutoSearchRollout.EVERYONE, None) is True


def test_admin_only_allows_admins_and_noauth_blocks_basic() -> None:
    # admin yes; basic NO (the gate that protects the staged rollout); the
    # no-auth/superuser context (user is None) is treated as admin, matching
    # get_personas' convention so a no-auth deployment isn't locked out.
    assert _auto_search_allowed(AutoSearchRollout.ADMIN_ONLY, _ADMIN) is True
    assert _auto_search_allowed(AutoSearchRollout.ADMIN_ONLY, _BASIC) is False
    assert _auto_search_allowed(AutoSearchRollout.ADMIN_ONLY, None) is True


def test_default_rollout_is_admin_only() -> None:
    # A fresh deploy must land admin-only with no manual seeding — this is what
    # makes "ship to prod, fix metadata, then flip to everyone" safe.
    from danswer.server.settings.models import Settings

    assert Settings().auto_search_rollout == AutoSearchRollout.ADMIN_ONLY
