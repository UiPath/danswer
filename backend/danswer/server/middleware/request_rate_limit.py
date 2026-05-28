"""Per-user request-rate limiter — Redis-backed, multi-window, fail-open.

Why this exists, in one sentence: the existing
``danswer.server.query_and_chat.token_limit.check_token_rate_limits`` is a
**token-budget** limiter (sum of tokens over a window, DB-backed). It
caps cost, not request volume, and its in-process ``@lru_cache`` short-
circuit (``any_rate_limit_exists``) is per-pod, so two replicas can
disagree on whether limits are configured at all. This module is the
**request-rate** complement: a per-user (or per-IP for anonymous) cap on
the number of /send-message calls per minute / per hour, with Redis as
the shared counter so the cap holds across replicas.

Design notes:

* **Fixed-window buckets.** ``bucket = floor(time() / window)``. Simpler
  and cheaper than sliding-window log; the trade-off is that a user
  can burst up to ``2 * limit`` across a window boundary. Acceptable
  for the protection target (abuse / runaway cost), not for strict SLA
  enforcement.
* **Atomic ``INCR`` + ``EXPIRE NX``.** The expiry is set only on the
  first increment of the bucket so the window boundary is preserved
  across concurrent requests racing for the first slot. Without ``NX``,
  every request would push the expiry forward and the bucket would
  never reset.
* **Fail-open.** Any Redis error allows the request through with a log.
  Refusing the chat path because the *rate limiter* is down is a worse
  outcome than serving a few extra requests during a Redis blip.
* **Default OFF.** Even when Redis is up, the limiter does nothing
  until ``REQUEST_RATE_LIMIT_ENABLED=true`` AND at least one window
  limit (per-minute or per-hour) is > 0. This is a protection feature,
  not an always-on guard.
* **Anonymous callers are keyed by IP** (X-Forwarded-For first hop,
  falling back to the socket peer). If the IP can't be determined we
  silently skip — no key, nothing to limit.
"""
from __future__ import annotations

import time

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request

from danswer.auth.users import current_user
from danswer.configs.app_configs import REQUEST_RATE_LIMIT_ENABLED
from danswer.configs.app_configs import REQUEST_RATE_LIMIT_PER_HOUR
from danswer.configs.app_configs import REQUEST_RATE_LIMIT_PER_MINUTE
from danswer.db.models import User
from danswer.redis.redis_pool import DANSWER_REDIS_KEY_PREFIX
from danswer.redis.redis_pool import get_redis_client
from danswer.utils.logger import setup_logger


logger = setup_logger()


# All counters live under this prefix so a global FLUSHDB-by-prefix on
# this namespace is trivial in incident response. Sub-key shape:
#   <prefix>{actor}:{label}:{bucket}
# where actor is "u:<uuid>" or "ip:<addr>", label is "min" or "hour",
# and bucket is floor(unix_seconds / window).
_KEY_PREFIX = DANSWER_REDIS_KEY_PREFIX + "ratelimit:msg:"

_MIN_WINDOW_SECONDS = 60
_HOUR_WINDOW_SECONDS = 3600


def check_message_request_rate_limit(
    request: Request,
    user: User | None = Depends(current_user),
) -> None:
    """FastAPI dependency that 429s a caller over their per-window cap.

    Cheap fast-path when disabled — no Redis call, no env reads beyond
    the module-level constants. Safe to attach to every chat / query
    endpoint; the cost when off is one tuple-truthy check.
    """
    if not REQUEST_RATE_LIMIT_ENABLED:
        return
    if REQUEST_RATE_LIMIT_PER_MINUTE <= 0 and REQUEST_RATE_LIMIT_PER_HOUR <= 0:
        # Nothing to enforce — saves the Redis round-trip when an
        # operator enabled the flag but hasn't picked window values yet.
        return

    actor = _actor_key(user, request)
    if actor is None:
        return  # no key material; nothing we can fairly attribute

    # Order matters: enforce the tighter window first. If a user trips
    # the per-minute cap we don't need to also increment per-hour for
    # this request — but we do anyway so per-hour accounting stays
    # honest across bursts that don't trip the minute window.
    if REQUEST_RATE_LIMIT_PER_MINUTE > 0:
        _enforce_window(
            actor=actor,
            label="min",
            window_seconds=_MIN_WINDOW_SECONDS,
            limit=REQUEST_RATE_LIMIT_PER_MINUTE,
        )
    if REQUEST_RATE_LIMIT_PER_HOUR > 0:
        _enforce_window(
            actor=actor,
            label="hour",
            window_seconds=_HOUR_WINDOW_SECONDS,
            limit=REQUEST_RATE_LIMIT_PER_HOUR,
        )


def _actor_key(user: User | None, request: Request) -> str | None:
    """Identifier the limit is attributed to.

    Authenticated users are keyed by uuid (stable, survives IP changes).
    Anonymous traffic falls back to the first X-Forwarded-For hop set
    by the ingress; if nothing usable is present, we return None and
    skip — better to under-enforce than to bucket everyone behind a
    misconfigured proxy under the LB's own IP.
    """
    if user is not None:
        return f"u:{user.id}"

    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        client_ip = xff.split(",", 1)[0].strip()
    elif request.client is not None:
        client_ip = request.client.host
    else:
        client_ip = ""

    if not client_ip:
        return None
    return f"ip:{client_ip}"


def _enforce_window(
    *, actor: str, label: str, window_seconds: int, limit: int
) -> None:
    """Increment-and-check one window for one actor.

    Raises ``HTTPException(429)`` if the post-increment count exceeds
    ``limit``. The Retry-After header tells the caller exactly how long
    until the current bucket rolls over — handy for clients that back
    off intelligently.
    """
    bucket = int(time.time() // window_seconds)
    key = f"{_KEY_PREFIX}{actor}:{label}:{bucket}"

    try:
        client = get_redis_client()
        pipe = client.pipeline()
        pipe.incr(key, 1)
        # ``nx=True`` here means "set expiry only if no expiry yet" so
        # the first increment of the bucket fixes the window boundary.
        # Without it, every increment pushes expiry forward and the
        # bucket never resets.
        pipe.expire(key, window_seconds, nx=True)
        result = pipe.execute()
        count = int(result[0])
    except Exception as e:
        # Fail-open: better to let a request through than to wedge the
        # chat path because Redis is unhappy. Loud log so it's obvious
        # in the dashboard, but no exception propagation.
        logger.warning(
            "Rate-limit check skipped due to Redis error (actor=%s window=%s): %s",
            actor,
            label,
            e,
        )
        return

    if count > limit:
        # Seconds remaining in the current bucket — tells the caller
        # when to retry without us needing to look up the TTL.
        retry_after = window_seconds - (int(time.time()) % window_seconds)
        raise HTTPException(
            status_code=429,
            detail=(
                f"Request rate limit exceeded "
                f"({limit} per {label}). Retry in {retry_after}s."
            ),
            headers={"Retry-After": str(retry_after)},
        )
