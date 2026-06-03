"""Unit tests for ``danswer.redis.redis_pool``.

We exercise only what can be verified without a real Redis server:

  1. The pool is a process-wide singleton — calling ``get_redis_client``
     repeatedly does not build a new ``ConnectionPool`` each time.
  2. ``reset_pool_for_tests`` forces the next ``get_redis_client`` to
     rebuild — important so other tests can swap env vars and observe
     the change.
  3. The global key prefix is the documented value. If this ever
     changes silently it would orphan every cached entry in production
     on the next deploy; lock it down with a string equality assertion.

Live socket-level behaviour (pool sizing, TCP timeouts, SSL handshake)
is intentionally out of scope here — those need an integration test
against a real Redis.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from danswer.redis import redis_pool


class TestRedisPool(unittest.TestCase):
    def setUp(self) -> None:
        # Each test starts with a fresh, unbuilt pool so the singleton
        # state from earlier tests can't bleed in.
        redis_pool.reset_pool_for_tests()

    def tearDown(self) -> None:
        redis_pool.reset_pool_for_tests()

    def test_prefix_is_stable(self) -> None:
        """The on-the-wire key prefix is part of the persistence
        contract — every cached entry in production starts with it.
        Renaming it requires intentional migration, not a drive-by edit.
        """
        self.assertEqual(redis_pool.DANSWER_REDIS_KEY_PREFIX, "danswer:")

    def test_pool_built_lazily_and_reused(self) -> None:
        """``get_redis_client`` must build the pool on first use and
        reuse it after. We assert this by counting calls to the pool
        constructor under a patch.
        """
        with patch.object(
            redis_pool, "ConnectionPool", wraps=redis_pool.ConnectionPool
        ) as mock_pool:
            client_a = redis_pool.get_redis_client()
            client_b = redis_pool.get_redis_client()
            client_c = redis_pool.get_redis_client()

        self.assertEqual(
            mock_pool.call_count,
            1,
            "ConnectionPool should be constructed exactly once across "
            "repeated get_redis_client() calls",
        )
        # Different Redis() instances are fine — they share the pool.
        self.assertIs(
            client_a.connection_pool,
            client_b.connection_pool,
            "all clients must share the singleton pool",
        )
        self.assertIs(client_b.connection_pool, client_c.connection_pool)

    def test_reset_for_tests_drops_singleton(self) -> None:
        """After ``reset_pool_for_tests`` the next ``get_redis_client``
        must rebuild — otherwise tests can't observe config changes.
        """
        with patch.object(
            redis_pool, "ConnectionPool", wraps=redis_pool.ConnectionPool
        ) as mock_pool:
            redis_pool.get_redis_client()
            redis_pool.reset_pool_for_tests()
            redis_pool.get_redis_client()

        self.assertEqual(
            mock_pool.call_count,
            2,
            "reset_pool_for_tests should force the next call to rebuild",
        )


if __name__ == "__main__":
    unittest.main()
