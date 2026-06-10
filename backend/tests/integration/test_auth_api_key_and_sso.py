"""Integration tests for the auth gate — both flows must keep working:

  1. SSO / session: a browser request with no session is rejected (403), which
     is what drives the OIDC login flow. This must NOT be weakened.
  2. API key: these are service credentials for *automation* and intentionally
     do NOT map to a `User`. A request carrying a valid `X-API-Key` is authorized
     as an anonymous service caller (`user=None`, which endpoints already handle)
     instead of being 403'd into the SSO flow.

Regression context: enabling OIDC (AUTH_TYPE=oidc) flipped DISABLE_AUTH off, so
`current_user` started 403'ing api-key requests (they have no session and the
keys don't resolve to a user). These tests lock the contract so future changes
to auth or SSO don't silently break either flow.

Style matches the other tests in this dir: drive the real dependency functions
directly with stubs (no live server / DB).
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from starlette.datastructures import Headers

from danswer.auth import api_key as api_key_mod
from danswer.auth import users as users_mod
from danswer.auth.api_key import request_has_valid_api_key
from danswer.auth.schemas import UserRole


def _run(coro):  # avoid a hard pytest-asyncio dependency
    return asyncio.run(coro)


def _request(headers: dict[str, str]) -> SimpleNamespace:
    # Real Starlette Headers => case-insensitive lookup, exactly like a request
    # (the Postman collection sends lowercase "x-api-key").
    return SimpleNamespace(headers=Headers(headers))


class _StubDB:
    """Stands in for the Session: `.scalar()` returns a row iff `found`."""

    def __init__(self, found: bool) -> None:
        self._found = found

    def scalar(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(id=1, user_id="00000000-0000-0000-0000-000000000000") if self._found else None


def _user(*, verified: bool = True, role: UserRole = UserRole.BASIC) -> SimpleNamespace:
    return SimpleNamespace(is_verified=verified, role=role, email="svc@example.com")


@pytest.fixture(autouse=True)
def _clear_api_key_cache():
    # validate_api_key / request_has_valid_api_key share a module-level TTLCache;
    # clear it so tests don't leak validity into each other.
    api_key_mod.cache.clear()
    yield
    api_key_mod.cache.clear()


@pytest.fixture
def auth_enforced(monkeypatch):
    """Force the 'auth enabled' world (e.g. OIDC) deterministically.

    `double_check_user`'s `optional` default is captured from DISABLE_AUTH at
    import time, so we pin it to False here regardless of the test env's
    AUTH_TYPE. `current_admin_user` reads DISABLE_AUTH at call time, so patch
    that too.
    """
    monkeypatch.setattr(users_mod.double_check_user, "__defaults__", (False,))
    monkeypatch.setattr(users_mod, "DISABLE_AUTH", False)


# --------------------------------------------------------------------------- #
# request_has_valid_api_key — the validity check itself
# --------------------------------------------------------------------------- #
def test_valid_api_key_header_is_accepted():
    assert request_has_valid_api_key(_request({"X-API-Key": "k"}), _StubDB(found=True)) is True


def test_lowercase_header_is_accepted():
    # Postman sends "x-api-key"; header lookup must be case-insensitive.
    assert request_has_valid_api_key(_request({"x-api-key": "k"}), _StubDB(found=True)) is True


def test_unknown_api_key_is_rejected():
    assert request_has_valid_api_key(_request({"X-API-Key": "nope"}), _StubDB(found=False)) is False


def test_missing_header_is_not_valid():
    assert request_has_valid_api_key(_request({}), _StubDB(found=True)) is False


def test_empty_header_value_is_not_valid():
    assert request_has_valid_api_key(_request({"X-API-Key": ""}), _StubDB(found=True)) is False


# --------------------------------------------------------------------------- #
# current_user — the gate endpoints actually depend on (auth enforced)
# --------------------------------------------------------------------------- #
def test_valid_api_key_authorizes_as_anonymous_service_caller(auth_enforced):
    # The fix: valid key, no session -> authorized with user=None (no 403/SSO).
    result = _run(current_user_call(_request({"x-api-key": "k"}), user=None, db=_StubDB(found=True)))
    assert result is None


def test_invalid_api_key_is_rejected(auth_enforced):
    with pytest.raises(HTTPException) as exc:
        _run(current_user_call(_request({"x-api-key": "bad"}), user=None, db=_StubDB(found=False)))
    assert exc.value.status_code == 403


def test_no_session_and_no_api_key_is_rejected_so_sso_still_triggers(auth_enforced):
    # The SSO guard: browser request, no session, no key -> 403 (drives login).
    with pytest.raises(HTTPException) as exc:
        _run(current_user_call(_request({}), user=None, db=_StubDB(found=False)))
    assert exc.value.status_code == 403


def test_session_user_still_authenticates(auth_enforced):
    # A real (verified) OIDC/session user must still pass, key or not.
    u = _user(verified=True)
    assert _run(current_user_call(_request({}), user=u, db=_StubDB(found=False))) is u


# --------------------------------------------------------------------------- #
# current_admin_user — a key alone must NOT grant admin
# --------------------------------------------------------------------------- #
def test_api_key_does_not_grant_admin(auth_enforced):
    # api-key request resolves to user=None -> admin gate must still 403.
    with pytest.raises(HTTPException) as exc:
        _run(users_mod.current_admin_user(user=None))
    assert exc.value.status_code == 403


def test_admin_user_passes_admin_gate(auth_enforced):
    admin = _user(role=UserRole.ADMIN)
    assert _run(users_mod.current_admin_user(user=admin)) is admin


def test_basic_user_blocked_from_admin_gate(auth_enforced):
    with pytest.raises(HTTPException) as exc:
        _run(users_mod.current_admin_user(user=_user(role=UserRole.BASIC)))
    assert exc.value.status_code == 403


# helper: call current_user with explicit args (bypassing FastAPI DI defaults)
async def current_user_call(request, user, db):  # noqa: ANN001
    return await users_mod.current_user(request=request, user=user, db_session=db)
