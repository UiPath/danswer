"""Unit tests for the Redis-backed per-user request rate limiter.

What we lock down here is the contract a chat endpoint relies on when it
attaches ``Depends(check_message_request_rate_limit)``:

  1. **Default off:** with the feature flag down OR both window limits
     at 0, the dependency must short-circuit before touching Redis.
     This matters because the dependency is mounted on the hot path of
     every chat message — any cost in the off case is paid on every
     request forever.
  2. **Per-window enforcement:** the Nth request through the same
     bucket exceeds the cap and 429s; the same caller in the next
     bucket gets a fresh window.
  3. **Per-user isolation:** two distinct users must not share counters
     even if their requests interleave in the same bucket.
  4. **Anonymous keying by IP:** unauth'd callers are bucketed by
     X-Forwarded-For first hop (matching the ingress shape), falling
     back to the socket peer; otherwise the dependency skips.
  5. **EXPIRE NX semantics:** the first ``INCR`` of a bucket sets the
     TTL; subsequent ``INCR`` calls must NOT extend it (a sliding TTL
     would make the bucket never reset and effectively cap *forever*
     after the first burst).
  6. **Fail-open:** any Redis error allows the request through. The
     limiter is protection, not authorization — a Redis blip is not a
     reason to wedge the chat path.
  7. **Retry-After header:** a 429 carries seconds-until-bucket-rollover
     so well-behaved clients can back off precisely.

Redis is mocked at the ``get_redis_client`` boundary; the FastAPI
``Request`` and ``User`` are dummy objects. No HTTP layer, no real
Redis — pure dependency-function tests.
"""
from __future__ import annotations

import unittest
import uuid
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

from fastapi import HTTPException

from danswer.server.middleware import request_rate_limit as rrl


# ---------- shared fakes ----------


class _FakePipeline:
    """Minimal stand-in for redis.client.Pipeline.

    We only need .incr, .expire, .execute — that's the full surface
    used in _enforce_window. We also remember every .expire(..., nx=)
    call so the NX-semantics test can inspect it.
    """

    def __init__(self, storage: dict[str, int], expiry: dict[str, bool]) -> None:
        self._storage = storage
        self._expiry = expiry
        self._ops: list[tuple[str, Any, Any]] = []

    def incr(self, key: str, amount: int = 1) -> "_FakePipeline":
        self._ops.append(("incr", key, amount))
        return self

    def expire(self, key: str, seconds: int, nx: bool = False) -> "_FakePipeline":
        self._ops.append(("expire", key, (seconds, nx)))
        return self

    def execute(self) -> list[Any]:
        results: list[Any] = []
        for op, key, arg in self._ops:
            if op == "incr":
                self._storage[key] = self._storage.get(key, 0) + int(arg)
                results.append(self._storage[key])
            elif op == "expire":
                seconds, nx = arg
                if nx and self._expiry.get(key):
                    results.append(False)  # already has TTL — refused
                else:
                    self._expiry[key] = True
                    results.append(True)
        self._ops.clear()
        return results


class _FakeRedis:
    """Fake Redis client exposing only the methods the limiter uses."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._has_expiry: dict[str, bool] = {}
        self.expire_calls: list[tuple[str, int, bool]] = []

    def pipeline(self) -> _FakePipeline:
        pipe = _FakePipeline(self._counters, self._has_expiry)
        # Wrap pipe.expire to record every call for inspection.
        original_expire = pipe.expire

        def recording_expire(key: str, seconds: int, nx: bool = False) -> Any:
            self.expire_calls.append((key, seconds, nx))
            return original_expire(key, seconds, nx=nx)

        pipe.expire = recording_expire  # type: ignore[method-assign]
        return pipe


def _make_request(
    headers: dict[str, str] | None = None, peer_host: str | None = None
) -> MagicMock:
    """Minimal Starlette Request stand-in."""
    req = MagicMock()
    req.headers = headers or {}
    req.client = MagicMock(host=peer_host) if peer_host is not None else None
    return req


def _make_user(uid: uuid.UUID | None = None) -> MagicMock:
    user = MagicMock()
    user.id = uid or uuid.uuid4()
    return user


# ---------- tests ----------


class TestRequestRateLimitDisabled(unittest.TestCase):
    """When disabled, the dependency must do nothing — not even
    construct a Redis client. The hot path can't afford ambient cost
    that callers thought they'd avoided by turning the flag off.
    """

    def test_flag_off_short_circuits_before_redis(self) -> None:
        request = _make_request()
        user = _make_user()
        with patch.object(rrl, "REQUEST_RATE_LIMIT_ENABLED", False), patch.object(
            rrl, "get_redis_client"
        ) as mock_client:
            rrl.check_message_request_rate_limit(request=request, user=user)
            mock_client.assert_not_called()

    def test_both_windows_zero_short_circuits_before_redis(self) -> None:
        """Flag on but no limits configured = nothing to enforce. The
        operator probably enabled the flag and hasn't picked numbers
        yet; we must not pay the Redis round-trip in that interim
        state.
        """
        request = _make_request()
        user = _make_user()
        with patch.object(rrl, "REQUEST_RATE_LIMIT_ENABLED", True), patch.object(
            rrl, "REQUEST_RATE_LIMIT_PER_MINUTE", 0
        ), patch.object(rrl, "REQUEST_RATE_LIMIT_PER_HOUR", 0), patch.object(
            rrl, "get_redis_client"
        ) as mock_client:
            rrl.check_message_request_rate_limit(request=request, user=user)
            mock_client.assert_not_called()


class TestRequestRateLimitEnforcement(unittest.TestCase):
    def _patch_enabled(self, per_min: int = 0, per_hour: int = 0) -> Any:
        """Helper: turn the limiter on with the given window caps."""
        return _MultiPatch(
            (rrl, "REQUEST_RATE_LIMIT_ENABLED", True),
            (rrl, "REQUEST_RATE_LIMIT_PER_MINUTE", per_min),
            (rrl, "REQUEST_RATE_LIMIT_PER_HOUR", per_hour),
        )

    def test_within_limit_allows_request(self) -> None:
        """Under the cap = no 429. Sanity, but also makes sure the
        ``count > limit`` boundary is strict (the Nth allowed request
        is the *limit*-th, not limit-minus-one).
        """
        fake = _FakeRedis()
        request = _make_request()
        user = _make_user()
        with self._patch_enabled(per_min=3), patch.object(
            rrl, "get_redis_client", return_value=fake
        ):
            for _ in range(3):
                rrl.check_message_request_rate_limit(request=request, user=user)
            # No exception raised — all three under the cap of 3.

    def test_request_above_cap_raises_429_with_retry_after(self) -> None:
        """The (limit+1)-th call in a bucket must 429, and the response
        must carry Retry-After. Clients without Retry-After back off
        with guesswork; we should hand them the exact answer.
        """
        fake = _FakeRedis()
        request = _make_request()
        user = _make_user()
        with self._patch_enabled(per_min=2), patch.object(
            rrl, "get_redis_client", return_value=fake
        ):
            rrl.check_message_request_rate_limit(request=request, user=user)
            rrl.check_message_request_rate_limit(request=request, user=user)
            with self.assertRaises(HTTPException) as ctx:
                rrl.check_message_request_rate_limit(request=request, user=user)
        self.assertEqual(ctx.exception.status_code, 429)
        retry_after = ctx.exception.headers and ctx.exception.headers.get("Retry-After")
        self.assertIsNotNone(retry_after)
        self.assertTrue(retry_after.isdigit())  # type: ignore[union-attr]
        # 0 < retry_after <= window. (Equal to window iff time landed
        # exactly on the boundary — possible but rare, allow it.)
        self.assertGreaterEqual(int(retry_after), 0)  # type: ignore[arg-type]
        self.assertLessEqual(int(retry_after), 60)  # type: ignore[arg-type]

    def test_two_users_have_independent_counters(self) -> None:
        """Distinct user UUIDs must NOT share a bucket. If they did, a
        loud user could 429 a quiet one.
        """
        fake = _FakeRedis()
        request = _make_request()
        alice = _make_user()
        bob = _make_user()
        with self._patch_enabled(per_min=1), patch.object(
            rrl, "get_redis_client", return_value=fake
        ):
            rrl.check_message_request_rate_limit(request=request, user=alice)
            # Bob's first request must succeed even though Alice already
            # used her one allowed call in this bucket.
            rrl.check_message_request_rate_limit(request=request, user=bob)
            # Alice's second request hits her cap — should 429.
            with self.assertRaises(HTTPException) as ctx:
                rrl.check_message_request_rate_limit(request=request, user=alice)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_next_bucket_resets_count(self) -> None:
        """When time advances past the window boundary, the bucket key
        changes (it's keyed by ``floor(time / window)``) and the new
        bucket starts at 0. Without this, the limit is forever rather
        than per-window.
        """
        fake = _FakeRedis()
        request = _make_request()
        user = _make_user()
        with self._patch_enabled(per_min=1), patch.object(
            rrl, "get_redis_client", return_value=fake
        ):
            with patch.object(rrl.time, "time", return_value=1_000_000.0):
                rrl.check_message_request_rate_limit(request=request, user=user)
                # Same bucket -> over cap.
                with self.assertRaises(HTTPException):
                    rrl.check_message_request_rate_limit(request=request, user=user)
            # Jump 90s — new minute bucket.
            with patch.object(rrl.time, "time", return_value=1_000_000.0 + 90):
                rrl.check_message_request_rate_limit(request=request, user=user)

    def test_expire_uses_nx_so_ttl_is_set_only_once(self) -> None:
        """Every ``INCR`` is paired with ``EXPIRE`` — but if NX weren't
        set, each increment would push the expiry forward and the
        bucket would never roll over. Lock down ``nx=True`` so a future
        refactor doesn't accidentally make every limited window become
        a permanent ban after the first burst.
        """
        fake = _FakeRedis()
        request = _make_request()
        user = _make_user()
        with self._patch_enabled(per_min=10), patch.object(
            rrl, "get_redis_client", return_value=fake
        ):
            for _ in range(3):
                rrl.check_message_request_rate_limit(request=request, user=user)
        # All EXPIRE calls used nx=True. (At least one happened.)
        self.assertGreater(len(fake.expire_calls), 0)
        for _key, _seconds, nx in fake.expire_calls:
            self.assertTrue(
                nx, "EXPIRE must use NX so TTL isn't extended on every INCR"
            )

    def test_anonymous_user_keyed_by_xff_first_hop(self) -> None:
        """Anonymous traffic keys on the first XFF hop (the real client
        IP behind nginx), not on the LB's own peer address. Otherwise
        every anonymous request would share one bucket.
        """
        fake = _FakeRedis()
        # Two distinct anonymous IPs in XFF.
        req_a = _make_request(headers={"x-forwarded-for": "10.1.1.1, 10.0.0.1"})
        req_b = _make_request(headers={"x-forwarded-for": "10.1.1.2, 10.0.0.1"})
        with self._patch_enabled(per_min=1), patch.object(
            rrl, "get_redis_client", return_value=fake
        ):
            rrl.check_message_request_rate_limit(request=req_a, user=None)
            # Different XFF first hop => different bucket, allowed.
            rrl.check_message_request_rate_limit(request=req_b, user=None)
            # Same XFF as req_a => second hit, exceeds cap.
            with self.assertRaises(HTTPException):
                rrl.check_message_request_rate_limit(request=req_a, user=None)

    def test_anonymous_with_no_ip_skips_silently(self) -> None:
        """If neither XFF nor a client peer is present, we have nothing
        to attribute the request to. Skipping is the only honest
        option — bucketing everyone under "" would silently flatten
        every anonymous client into one counter.
        """
        fake = _FakeRedis()
        request = _make_request(headers={}, peer_host=None)
        with self._patch_enabled(per_min=1), patch.object(
            rrl, "get_redis_client", return_value=fake
        ) as mock_client:
            # Call twice — both must pass; the limiter must not even
            # have constructed a key to enforce against.
            rrl.check_message_request_rate_limit(request=request, user=None)
            rrl.check_message_request_rate_limit(request=request, user=None)
            mock_client.assert_not_called()

    def test_redis_error_fails_open(self) -> None:
        """A pipeline that explodes (timeout, broken connection,
        whatever) must NOT raise out of the dependency. The chat path
        keeps serving — a request slipped past the limiter is better
        than a chat outage caused by the limiter itself.
        """
        bad_client = MagicMock()
        bad_client.pipeline.side_effect = RuntimeError("redis exploded")
        request = _make_request()
        user = _make_user()
        with self._patch_enabled(per_min=1), patch.object(
            rrl, "get_redis_client", return_value=bad_client
        ):
            # Two calls back-to-back — neither raises, because the
            # limiter swallows the Redis error.
            rrl.check_message_request_rate_limit(request=request, user=user)
            rrl.check_message_request_rate_limit(request=request, user=user)


class _MultiPatch:
    """Context manager that applies several ``patch.object`` patches at
    once. Used to make per-test "turn on the limiter with these
    windows" blocks readable.
    """

    def __init__(self, *patches: tuple[Any, str, Any]) -> None:
        self._patches = [patch.object(obj, attr, val) for obj, attr, val in patches]

    def __enter__(self) -> None:
        for p in self._patches:
            p.start()

    def __exit__(self, *exc: Any) -> None:
        for p in reversed(self._patches):
            p.stop()


if __name__ == "__main__":
    unittest.main()
