"""Unit tests for ``RedisCachedDynamicConfigStore`` — the read-through /
write-through Redis cache wrapper around any ``DynamicConfigStore``.

The behaviour we lock down here is the contract the rest of the app
relies on:

  1. **Read-through:** first ``load`` reads the inner store and
     repopulates Redis; subsequent ``load``s hit Redis only and never
     touch the inner store.
  2. **Write-through:** ``store`` writes the inner store first, then
     refreshes Redis with the new value + TTL so other replicas see
     the change without waiting for the TTL to expire.
  3. **Delete invalidates:** ``delete`` removes the inner row and clears
     Redis. Inner store is removed first — a Redis success that arrives
     before an inner failure must not leave Redis caching a value the
     source of truth no longer has.
  4. **Fail-open:** any ``RedisError`` is logged and silently swallowed.
     ``GET`` failures become misses; ``SET``/``DEL`` failures don't
     bubble up. The point is that a Redis outage degrades latency, not
     availability.
  5. **Encrypted values are never cached plaintext.** ``store(..., encrypt=True)``
     invalidates the Redis entry rather than writing plaintext into it,
     so the encryption-at-rest guarantee isn't silently bypassed.
  6. **Cache miss vs None:** the wrapper distinguishes "Redis returned
     ``nil``" (miss) from "Redis returned the JSON literal ``null``"
     (cached None value). Both look like Python ``None`` if you're
     careless; we test that a cached ``None`` is served from Redis
     without re-hitting the inner store.

The inner store and the Redis client are mocks — no real Postgres or
Redis required.
"""
from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import MagicMock

from redis import RedisError

from danswer.dynamic_configs.interface import ConfigNotFoundError
from danswer.dynamic_configs.store import RedisCachedDynamicConfigStore


# We can't import the real prefix without importing redis_pool, which
# imports app_configs and is fine — but expressing it here documents
# the on-disk key shape we expect.
_EXPECTED_PREFIX = "danswer:kv:"


def _make_inner() -> MagicMock:
    """Inner DynamicConfigStore mock with the methods we exercise."""
    inner = MagicMock()
    inner.store = MagicMock()
    inner.load = MagicMock()
    inner.delete = MagicMock()
    return inner


def _make_redis() -> MagicMock:
    """In-memory fake Redis covering get/set/delete only.

    Stores raw bytes the same way redis-py does, so JSON encode/decode
    in the wrapper actually runs.
    """
    storage: dict[str, bytes] = {}

    fake = MagicMock()

    def fake_get(key: str) -> bytes | None:
        return storage.get(key)

    def fake_set(key: str, value: Any, ex: int | None = None) -> bool:
        if isinstance(value, str):
            storage[key] = value.encode("utf-8")
        elif isinstance(value, bytes):
            storage[key] = value
        else:
            storage[key] = str(value).encode("utf-8")
        # ``ex`` is observed via the mock for the TTL assertion below.
        return True

    def fake_delete(*keys: str) -> int:
        removed = 0
        for k in keys:
            if k in storage:
                del storage[k]
                removed += 1
        return removed

    fake.get.side_effect = fake_get
    fake.set.side_effect = fake_set
    fake.delete.side_effect = fake_delete
    fake._storage = storage  # expose for assertions
    return fake


class TestRedisCachedDynamicConfigStore(unittest.TestCase):
    # ------------- read-through -------------

    def test_load_miss_then_hit_only_one_inner_read(self) -> None:
        """First ``load`` is a Redis miss → falls through to the inner
        store and repopulates Redis. The second ``load`` must serve from
        Redis alone — the inner store must NOT be touched again. This
        is the central performance promise of the cache.
        """
        inner = _make_inner()
        inner.load.return_value = {"feature_flag": True}
        redis = _make_redis()
        store = RedisCachedDynamicConfigStore(
            inner=inner, ttl_seconds=60, client_factory=lambda: redis
        )

        first = store.load("settings")
        second = store.load("settings")

        self.assertEqual(first, {"feature_flag": True})
        self.assertEqual(second, {"feature_flag": True})
        self.assertEqual(
            inner.load.call_count,
            1,
            "second load must come from Redis, not the inner store",
        )

    def test_load_populates_redis_with_ttl(self) -> None:
        """On a miss, the wrapper must SET into Redis with an expiry —
        otherwise the cache would never evict and a value written by
        another pod would be served stale forever.
        """
        inner = _make_inner()
        inner.load.return_value = {"a": 1}
        redis = _make_redis()
        store = RedisCachedDynamicConfigStore(
            inner=inner, ttl_seconds=120, client_factory=lambda: redis
        )

        store.load("k1")

        redis.set.assert_called_once()
        args, kwargs = redis.set.call_args
        self.assertEqual(args[0], _EXPECTED_PREFIX + "k1")
        # JSON-serialised payload, with the TTL kwarg matching the ctor.
        self.assertEqual(json.loads(args[1]), {"a": 1})
        self.assertEqual(kwargs.get("ex"), 120)

    def test_load_propagates_not_found_without_caching_miss(self) -> None:
        """If the inner store has nothing, the wrapper must raise
        ``ConfigNotFoundError`` and NOT cache the absence — negative
        caching has its own correctness gotchas (a later ``store`` would
        race with the stale "missing" entry), and the plan deliberately
        defers it.
        """
        inner = _make_inner()
        inner.load.side_effect = ConfigNotFoundError
        redis = _make_redis()
        store = RedisCachedDynamicConfigStore(
            inner=inner, ttl_seconds=60, client_factory=lambda: redis
        )

        with self.assertRaises(ConfigNotFoundError):
            store.load("absent")
        # No SET — we didn't cache the miss.
        redis.set.assert_not_called()

    def test_cached_none_is_distinguished_from_miss(self) -> None:
        """``None`` is a legal stored value (the KV store can hold a
        JSON ``null``). We must serve a cached ``null`` without falling
        through to the inner store — otherwise every read of a None
        value is effectively uncached.
        """
        inner = _make_inner()
        inner.load.return_value = None
        redis = _make_redis()
        store = RedisCachedDynamicConfigStore(
            inner=inner, ttl_seconds=60, client_factory=lambda: redis
        )

        first = store.load("nullable")  # miss → inner → cache
        second = store.load("nullable")  # hit

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(
            inner.load.call_count,
            1,
            "second load of a cached None must hit Redis, not the inner store",
        )

    # ------------- write-through / invalidation -------------

    def test_store_writes_inner_then_refreshes_redis(self) -> None:
        """``store`` must write the inner store first (source of truth),
        then refresh Redis. Order matters: a Redis success after an
        inner failure would leave Redis ahead of the source of truth.
        """
        inner = _make_inner()
        redis = _make_redis()
        call_order: list[str] = []
        inner.store.side_effect = lambda *a, **kw: call_order.append("inner")
        # Wrap the existing side_effect to record set ordering.
        original_set = redis.set.side_effect

        def recording_set(*a: Any, **kw: Any) -> Any:
            call_order.append("redis")
            return original_set(*a, **kw)

        redis.set.side_effect = recording_set

        store = RedisCachedDynamicConfigStore(
            inner=inner, ttl_seconds=30, client_factory=lambda: redis
        )
        store.store("settings", {"v": 7})

        inner.store.assert_called_once_with("settings", {"v": 7}, encrypt=False)
        self.assertEqual(
            call_order, ["inner", "redis"], "inner store must be written first"
        )
        # Subsequent load returns the new value from Redis only.
        inner.load.reset_mock()
        result = store.load("settings")
        self.assertEqual(result, {"v": 7})
        inner.load.assert_not_called()

    def test_encrypted_store_invalidates_redis(self) -> None:
        """``encrypt=True`` means "Postgres holds this encrypted." We
        must NOT mirror plaintext into Redis (which has no encryption
        guarantee), and we must invalidate any prior plaintext entry
        in case the value was just switched to encrypted.
        """
        inner = _make_inner()
        redis = _make_redis()
        # Pre-seed a stale plaintext entry to confirm it gets cleared.
        redis._storage[_EXPECTED_PREFIX + "secret"] = b'"old"'

        store = RedisCachedDynamicConfigStore(
            inner=inner, ttl_seconds=60, client_factory=lambda: redis
        )
        store.store("secret", "new-value", encrypt=True)

        inner.store.assert_called_once_with("secret", "new-value", encrypt=True)
        redis.set.assert_not_called()  # no plaintext mirror
        redis.delete.assert_called_once_with(_EXPECTED_PREFIX + "secret")
        self.assertNotIn(_EXPECTED_PREFIX + "secret", redis._storage)

    def test_delete_clears_inner_and_redis(self) -> None:
        """``delete`` clears both layers. Inner first — same ordering
        invariant as ``store``: Redis must never be cleaner than the
        source of truth.
        """
        inner = _make_inner()
        redis = _make_redis()
        redis._storage[_EXPECTED_PREFIX + "k"] = b'{"x":1}'
        call_order: list[str] = []
        inner.delete.side_effect = lambda *a, **kw: call_order.append("inner")
        original_delete = redis.delete.side_effect

        def recording_delete(*a: Any, **kw: Any) -> Any:
            call_order.append("redis")
            return original_delete(*a, **kw)

        redis.delete.side_effect = recording_delete

        store = RedisCachedDynamicConfigStore(
            inner=inner, ttl_seconds=60, client_factory=lambda: redis
        )
        store.delete("k")

        inner.delete.assert_called_once_with("k")
        self.assertEqual(call_order, ["inner", "redis"])
        self.assertNotIn(_EXPECTED_PREFIX + "k", redis._storage)

    # ------------- fail-open behaviour -------------

    def test_redis_get_error_falls_through_to_inner(self) -> None:
        """Redis ``GET`` exploding (timeout, conn refused, network
        partition) must NOT propagate. The wrapper degrades to a plain
        read against the inner store so a Redis outage costs latency,
        not availability.
        """
        inner = _make_inner()
        inner.load.return_value = "from-postgres"
        redis = MagicMock()
        redis.get.side_effect = RedisError("connection refused")

        store = RedisCachedDynamicConfigStore(
            inner=inner, ttl_seconds=60, client_factory=lambda: redis
        )
        result = store.load("k")

        self.assertEqual(result, "from-postgres")
        inner.load.assert_called_once_with("k")

    def test_redis_set_error_does_not_break_store(self) -> None:
        """SET failing must not bubble out of ``store`` — the inner
        write already succeeded, returning an error to the caller would
        lie about the durability of the write.
        """
        inner = _make_inner()
        redis = MagicMock()
        redis.set.side_effect = RedisError("OOM")

        store = RedisCachedDynamicConfigStore(
            inner=inner, ttl_seconds=60, client_factory=lambda: redis
        )
        # Must not raise.
        store.store("k", {"v": 1})
        inner.store.assert_called_once()

    def test_corrupt_cache_entry_treated_as_miss(self) -> None:
        """If something else wrote non-JSON bytes under our key (legacy
        format, manual ``SET``, race during a schema change), the next
        read must not crash — it must fall through to the inner store
        and overwrite the corrupt entry on the next SET.
        """
        inner = _make_inner()
        inner.load.return_value = "ok"
        redis = _make_redis()
        redis._storage[_EXPECTED_PREFIX + "k"] = b"not-json-at-all"

        store = RedisCachedDynamicConfigStore(
            inner=inner, ttl_seconds=60, client_factory=lambda: redis
        )
        result = store.load("k")

        self.assertEqual(result, "ok")
        inner.load.assert_called_once_with("k")
        # Wrapper repopulated Redis with the good value.
        self.assertEqual(json.loads(redis._storage[_EXPECTED_PREFIX + "k"]), "ok")

    def test_non_json_serialisable_value_skips_cache_but_inner_still_written(
        self,
    ) -> None:
        """If a caller hands us a Python object json can't serialise
        (sets, complex numbers, etc.), the inner store still gets it —
        Redis just silently skips the cache write. The inner is the
        source of truth; the cache is best-effort.
        """
        inner = _make_inner()
        redis = _make_redis()
        store = RedisCachedDynamicConfigStore(
            inner=inner, ttl_seconds=60, client_factory=lambda: redis
        )

        # set() is not JSON-serialisable.
        store.store("k", {1, 2, 3})  # type: ignore[arg-type]

        inner.store.assert_called_once()
        redis.set.assert_not_called()


if __name__ == "__main__":
    unittest.main()
