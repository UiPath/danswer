"""Process-local Redis client and connection pool.

This fork is single-tenant, so there is no per-tenant key prefixing
(upstream Onyx's ``TenantRedisClient`` is intentionally not ported).
Instead, all keys written by this codebase must namespace themselves
under :data:`DANSWER_REDIS_KEY_PREFIX` so a shared Redis (or a
later multi-app deployment) does not collide.

The pool is lazily built on first use and reused for the life of the
process. The :func:`get_redis_client` helper hands out a thin ``Redis``
wrapper around the shared pool — cheap to call repeatedly, no need to
cache the result at call sites.

Errors are NOT swallowed here. Callers that want to fail open (e.g. the
KV cache layer, the rate limiter) wrap their own try/except — that
choice belongs to the caller, not the connection helper.
"""
from __future__ import annotations

import threading
from typing import Any

import redis
from redis import ConnectionPool
from redis import Redis

from danswer.configs.app_configs import REDIS_DB_NUMBER
from danswer.configs.app_configs import REDIS_HEALTH_CHECK_INTERVAL
from danswer.configs.app_configs import REDIS_HOST
from danswer.configs.app_configs import REDIS_PASSWORD
from danswer.configs.app_configs import REDIS_POOL_MAX_CONNECTIONS
from danswer.configs.app_configs import REDIS_PORT
from danswer.configs.app_configs import REDIS_SOCKET_TIMEOUT_SECONDS
from danswer.configs.app_configs import REDIS_SSL
from danswer.utils.logger import setup_logger


logger = setup_logger()

# Every key written by this codebase MUST start with this prefix. Sub-modules
# append their own namespace (e.g. ``DANSWER_REDIS_KEY_PREFIX + "kv:"`` in the
# KV cache). Keeping the namespace centralised here avoids the
# "two callers picked the same key by accident" footgun.
DANSWER_REDIS_KEY_PREFIX = "danswer:"


_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _build_pool() -> ConnectionPool:
    """Construct the connection pool from the current env-driven config.

    Kept private so callers can't accidentally instantiate parallel pools.
    """
    kwargs: dict[str, Any] = {
        "host": REDIS_HOST,
        "port": REDIS_PORT,
        "db": REDIS_DB_NUMBER,
        "max_connections": REDIS_POOL_MAX_CONNECTIONS,
        "health_check_interval": REDIS_HEALTH_CHECK_INTERVAL,
        "socket_timeout": REDIS_SOCKET_TIMEOUT_SECONDS,
        "socket_connect_timeout": REDIS_SOCKET_TIMEOUT_SECONDS,
        "socket_keepalive": True,
        "retry_on_timeout": True,
        # We store JSON / counters as bytes; consumers decode as needed.
        # decode_responses=False keeps us out of accidental str/bytes mixups.
        "decode_responses": False,
    }
    if REDIS_PASSWORD:
        kwargs["password"] = REDIS_PASSWORD
    if REDIS_SSL:
        # SSLConnection picks up REDIS_SSL_CA_CERTS / REDIS_SSL_CERT_REQS
        # from env via the redis-py default — extend here if needed.
        kwargs["connection_class"] = redis.SSLConnection

    logger.info(
        "Building Redis ConnectionPool host=%s port=%s db=%s ssl=%s max=%s",
        REDIS_HOST,
        REDIS_PORT,
        REDIS_DB_NUMBER,
        REDIS_SSL,
        REDIS_POOL_MAX_CONNECTIONS,
    )
    return ConnectionPool(**kwargs)


def get_redis_client() -> Redis:
    """Return a thin Redis client backed by the shared, lazily-built pool.

    Safe to call from any thread; uses double-checked locking so the pool
    is constructed exactly once per process.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _build_pool()
    return Redis(connection_pool=_pool)


def reset_pool_for_tests() -> None:
    """Drop the cached pool so the next ``get_redis_client`` rebuilds it.

    Tests only — never call this in production code. Lets a test mutate
    env vars (host/port/etc.) and observe the effect on the next call.
    """
    global _pool
    with _pool_lock:
        _pool = None
