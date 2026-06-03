"""Per-user persona ("assistant") list cache, Redis-backed.

The "Manage Assistants" tile in the chat UI fires
``GET /persona`` → ``server/features/persona/api.py::list_personas`` →
``db/persona.py::get_personas(user_id, …)``. That query is a multi-OR
permission filter over ``Persona`` joined with ``Persona__User``,
``Persona__UserGroup`` and ``User__UserGroup`` (the user's groups). It
runs once per user per page-load; at hundreds of users opening chat
around the same time, the burst hits the DB connection pool harder
than it deserves to.

This module shifts the work to Redis with a **global cache + Python
filter** shape:

  ``personas:all:not_deleted``         — JSON list of every visible
                                         ``PersonaSnapshot``. Shared
                                         across users, so 200 concurrent
                                         first-clicks become ~1 DB
                                         query rather than 200.

  ``personas:groups:{user_id}``        — JSON list of the user's group
                                         ids; cheap one-row indexed
                                         lookup but worth caching since
                                         it's hit on every persona-list
                                         call.

Because ``PersonaSnapshot`` already carries the permission inputs
(``is_public``, ``users``, ``groups``), the filter runs in Python on
the cached list:

    persona.is_public
    OR user_id in {u.id for u in persona.users}      # direct grant
    OR (user_group_ids ∩ set(persona.groups))         # group grant

This mirrors the SQL OR-block in :func:`danswer.db.persona.get_personas`
exactly — the parity is locked down by tests.

**Invalidation:** explicit, write-through. Every mutation that affects
``Persona`` / ``Persona__User`` / ``Persona__UserGroup`` calls
:func:`invalidate_personas_all` after commit; every change to
``User__UserGroup`` calls :func:`invalidate_user_groups(user_id)`. The
``PERSONA_CACHE_TTL_SECONDS`` (24 h default) is *only* a long-tail
safety net for missed busts — the primary mechanism is explicit.

**Fail-open**: any Redis error logs and falls through to a direct DB
read. A Redis outage degrades latency, not availability.

**Default OFF**: ``PERSONA_CACHE_ENABLED=false`` keeps the existing
direct-DB path. Enable per environment once Redis is reachable.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from danswer.configs.app_configs import PERSONA_CACHE_ENABLED
from danswer.configs.app_configs import PERSONA_CACHE_TTL_SECONDS
from danswer.db.models import User__UserGroup
from danswer.redis.redis_pool import DANSWER_REDIS_KEY_PREFIX
from danswer.redis.redis_pool import get_redis_client
from danswer.server.features.persona.models import PersonaSnapshot
from danswer.utils.logger import setup_logger


logger = setup_logger()


# Single shared key for the "all non-deleted personas" snapshot list. Any
# Persona / Persona__User / Persona__UserGroup mutation must invalidate this.
_PERSONAS_ALL_KEY = DANSWER_REDIS_KEY_PREFIX + "personas:all:not_deleted"

# Per-user namespace for cached group memberships. User__UserGroup mutations
# must invalidate the affected user(s).
_USER_GROUPS_KEY_PREFIX = DANSWER_REDIS_KEY_PREFIX + "personas:groups:"


# ---------------------------------------------------------------------------
# Public API — read path
# ---------------------------------------------------------------------------


def get_personas_for_user_cached(
    user_id: UUID | None,
    db_session: Session,
    include_deleted: bool = False,
) -> list[PersonaSnapshot]:
    """Return the persona list visible to ``user_id`` as ``PersonaSnapshot``s.

    Routing:

    * Cache disabled OR ``include_deleted=True`` → direct DB read via the
      existing :func:`danswer.db.persona.get_personas`. The
      ``include_deleted`` case is rare admin-only and we deliberately
      don't cache it — keeping the cache key set small avoids accidental
      mis-keying on the hot path.
    * ``user_id is None`` (admin call) → return the global cached list
      unfiltered.
    * Authenticated user → load the global list + user's groups from
      cache (or DB on miss), apply the Python permission filter.
    """
    # Local import to keep this module importable from `db.persona` itself
    # without a circular import — `get_personas` is the fallback only.
    from danswer.db.persona import get_personas

    if not PERSONA_CACHE_ENABLED or include_deleted:
        personas = get_personas(
            user_id=user_id,
            db_session=db_session,
            include_deleted=include_deleted,
        )
        return [PersonaSnapshot.from_model(p) for p in personas]

    all_snapshots = _get_all_personas_cached(db_session)
    if user_id is None:
        # Admin / no-auth path: no permission filter needed.
        return all_snapshots

    user_group_ids = _get_user_group_ids_cached(user_id, db_session)
    return _filter_personas_for_user(all_snapshots, user_id, user_group_ids)


# ---------------------------------------------------------------------------
# Public API — invalidation
# ---------------------------------------------------------------------------


def invalidate_personas_all() -> None:
    """Drop the cached global persona list.

    Call this *after* ``db_session.commit()`` in any mutation that
    changes ``Persona``, ``Persona__User``, or ``Persona__UserGroup``.
    Before-commit invalidation has a stale-cache-fill race: a concurrent
    reader between bust and commit would refill the cache with the
    pre-mutation snapshot.

    Cheap when the cache is disabled — short-circuits before any Redis
    call so mutation paths don't pay an ambient cost.
    """
    if not PERSONA_CACHE_ENABLED:
        return
    try:
        get_redis_client().delete(_PERSONAS_ALL_KEY)
    except Exception as e:
        # Fail-open — TTL safety net will heal eventually. Loud log so
        # the dashboard catches a persistent Redis outage.
        logger.warning("invalidate_personas_all: Redis DEL failed: %s", e)


def invalidate_user_groups(user_id: UUID) -> None:
    """Drop ``user_id``'s cached group-membership list.

    Call this when ``User__UserGroup`` rows for the user are inserted
    or removed. Same after-commit ordering rule as
    :func:`invalidate_personas_all`.
    """
    if not PERSONA_CACHE_ENABLED:
        return
    try:
        get_redis_client().delete(_USER_GROUPS_KEY_PREFIX + str(user_id))
    except Exception as e:
        logger.warning(
            "invalidate_user_groups(user_id=%s): Redis DEL failed: %s",
            user_id,
            e,
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _get_all_personas_cached(db_session: Session) -> list[PersonaSnapshot]:
    """Load all non-deleted personas as ``PersonaSnapshot``s.

    Cache hit: deserialize the stored JSON straight back into Pydantic
    models — no DB call.
    Cache miss / Redis error: fall through to the existing
    :func:`get_personas` (with ``user_id=None``) so the source of truth
    is reused.
    """
    hit, cached = _safe_get(_PERSONAS_ALL_KEY)
    if hit and isinstance(cached, list):
        try:
            return [PersonaSnapshot.parse_obj(d) for d in cached]
        except Exception as e:
            # Pydantic schema drift (e.g. a new required field was added
            # since the entry was cached) — treat as a miss so the next
            # read repopulates with the current schema.
            logger.warning(
                "Cached persona list failed PersonaSnapshot parse, refilling: %s",
                e,
            )

    from danswer.db.persona import get_personas

    personas = get_personas(
        user_id=None,
        db_session=db_session,
        include_deleted=False,
    )
    snapshots = [PersonaSnapshot.from_model(p) for p in personas]

    # Round-trip through PersonaSnapshot.json() so nested types (UUID,
    # enums, datetimes) get the same serializer Pydantic uses on the wire.
    payload: list[Any] = [json.loads(s.json()) for s in snapshots]
    _safe_set(_PERSONAS_ALL_KEY, payload)
    return snapshots


def _get_user_group_ids_cached(user_id: UUID, db_session: Session) -> list[int]:
    """Return the ``user_group_id``s the user belongs to.

    Tiny indexed lookup — caching wins not on per-call latency but on
    aggregate, since it's hit on every persona-list call across all
    users.
    """
    key = _USER_GROUPS_KEY_PREFIX + str(user_id)
    hit, cached = _safe_get(key)
    if hit and isinstance(cached, list):
        return [int(x) for x in cached]

    rows = db_session.scalars(
        select(User__UserGroup.user_group_id).where(User__UserGroup.user_id == user_id)
    ).all()
    group_ids = [int(r) for r in rows]
    _safe_set(key, group_ids)
    return group_ids


def _filter_personas_for_user(
    personas: list[PersonaSnapshot],
    user_id: UUID,
    user_group_ids: list[int],
) -> list[PersonaSnapshot]:
    """Apply the same OR-filter ``get_personas`` runs in SQL.

    SQL:
        Persona.is_public
        OR Persona.id IN (Persona__User where user_id = U)
        OR Persona.id IN (Persona__UserGroup
                          where user_group_id IN <U's groups>)

    The parity vs SQL is covered by tests with representative permission
    shapes; if you change one side, change the other.
    """
    user_group_set = set(user_group_ids)
    out: list[PersonaSnapshot] = []
    for p in personas:
        if p.is_public:
            out.append(p)
            continue
        if any(u.id == user_id for u in p.users):
            out.append(p)
            continue
        if user_group_set.intersection(p.groups):
            out.append(p)
            continue
    return out


# ---- Fail-open Redis helpers (mirror the P1 cache module's posture) ----


def _safe_get(key: str) -> tuple[bool, Any]:
    """Return ``(hit, value)``. ``hit=False`` covers miss AND any Redis
    or decode error — the caller treats them all as "go to the DB".
    """
    try:
        raw = get_redis_client().get(key)
    except Exception as e:
        logger.warning("persona_cache: Redis GET failed for %s: %s", key, e)
        return (False, None)
    if raw is None:
        return (False, None)
    try:
        return (True, json.loads(raw))
    except (TypeError, ValueError) as e:
        logger.warning("persona_cache: corrupt entry at %s, ignoring: %s", key, e)
        return (False, None)


def _safe_set(key: str, val: Any) -> None:
    try:
        payload = json.dumps(val)
    except (TypeError, ValueError) as e:
        # Defensive — _get_all_personas_cached/_get_user_group_ids_cached
        # only ever cache JSON-clean values. If this fires the cache is
        # silently skipped and the inner read still served the caller.
        logger.warning("persona_cache: skipping non-JSON value at %s: %s", key, e)
        return
    try:
        get_redis_client().set(key, payload, ex=PERSONA_CACHE_TTL_SECONDS)
    except Exception as e:
        logger.warning("persona_cache: Redis SET failed for %s: %s", key, e)
