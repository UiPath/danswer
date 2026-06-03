"""Global document-set list cache, Redis-backed (MIT-scoped).

The chat-page bundle fires ``GET /document-set`` →
``server/features/document_set/api.py::list_document_sets`` →
``db/document_set.py::fetch_user_document_sets`` on *every* page load.
That read is a multi-join (DocumentSet ⋈ cc-pair mapping ⋈
ConnectorCredentialPair). At a few hundred users clicking around chat it
adds avoidable DB-pool pressure.

In Danswer **MIT**, document sets are *not* permission-filtered — every
user sees the same full list (they're organizational; the documents
themselves are permission-enforced at search time). So one **global**
cached list is correct for everyone, and 200 concurrent first-loads
collapse to a single DB query.

This module has **no dependency on the EE package** (different license).
It only reads the MIT-core flag ``global_version.get_is_ee_version()`` to
stay safe: if a deployment enables EE, ``fetch_user_document_sets`` starts
filtering per user, at which point a shared global list would leak sets
across users — so under EE we simply **bypass the cache** and read the DB
directly. Nothing here imports ``ee.*``; the global build uses the
``user_id=None`` path, which the core resolves to the MIT base query
without going through the versioned (EE) dispatch at all.

**Invalidation:** write-through. Every committing mutation in
``db/document_set.py`` calls :func:`invalidate_document_sets_all` after
commit (a single ``DEL`` of the global key). The
``DOCUMENT_SET_CACHE_TTL_SECONDS`` backstop heals any missed bust;
staleness is cosmetic (names/membership in the UI list).

**Fail-open**: any Redis error logs and falls through to a direct DB
build. **Default OFF**: ``DOCUMENT_SET_CACHE_ENABLED=false``.
"""
from __future__ import annotations

import json
from typing import Any
from typing import cast
from uuid import UUID

from sqlalchemy.orm import Session

from danswer.configs.app_configs import DOCUMENT_SET_CACHE_ENABLED
from danswer.configs.app_configs import DOCUMENT_SET_CACHE_TTL_SECONDS
from danswer.redis.redis_pool import DANSWER_REDIS_KEY_PREFIX
from danswer.redis.redis_pool import get_redis_client
from danswer.server.features.document_set.models import DocumentSet
from danswer.utils.logger import setup_logger
from danswer.utils.variable_functionality import global_version


logger = setup_logger()


# Single shared key — the full document-set list, identical for all users in
# MIT. Any document-set mutation must invalidate it.
_DOC_SETS_ALL_KEY = DANSWER_REDIS_KEY_PREFIX + "document_sets:all"


# ---------------------------------------------------------------------------
# Public API — read path
# ---------------------------------------------------------------------------


def get_document_sets_for_user_cached(
    user_id: UUID | None, db_session: Session
) -> list[DocumentSet]:
    """Return the ``DocumentSet`` list for ``user_id``.

    * Cache disabled, OR EE enabled (per-user filtering) → direct DB build
      for this user. The EE bypass avoids serving one user's filtered list
      to another; we never import EE, only check the MIT-core version flag.
    * MIT + enabled → the shared global list (built once, reused by all).
    """
    if not DOCUMENT_SET_CACHE_ENABLED or global_version.get_is_ee_version():
        return _build(user_id, db_session)

    hit, cached = _safe_get(_DOC_SETS_ALL_KEY)
    if hit and isinstance(cached, list):
        try:
            return [DocumentSet.parse_obj(d) for d in cached]
        except Exception as e:
            # Schema drift since the entry was cached — treat as a miss.
            logger.warning(
                "Cached document-set list failed DocumentSet parse, refilling: %s", e
            )

    # Build the global list via the user_id=None path — in the core this is
    # the MIT base query (all sets), and it never touches the versioned/EE
    # dispatch.
    result = _build(None, db_session)
    _safe_set(_DOC_SETS_ALL_KEY, [json.loads(ds.json()) for ds in result])
    return result


# ---------------------------------------------------------------------------
# Public API — invalidation
# ---------------------------------------------------------------------------


def invalidate_document_sets_all() -> None:
    """Drop the cached global document-set list.

    Call *after* ``db_session.commit()`` in any document-set mutation.
    Cheap no-op when the cache is disabled.
    """
    if not DOCUMENT_SET_CACHE_ENABLED:
        return
    try:
        get_redis_client().delete(_DOC_SETS_ALL_KEY)
    except Exception as e:
        # Fail-open — the TTL backstop heals it. Loud log for a persistent
        # Redis outage.
        logger.warning("invalidate_document_sets_all: Redis DEL failed: %s", e)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build(user_id: UUID | None, db_session: Session) -> list[DocumentSet]:
    """Build the ``DocumentSet`` list exactly as the endpoint did.

    Local imports keep this module free of an import cycle: ``db.document_set``
    imports :func:`invalidate_document_sets_all` from here at module load.
    """
    from danswer.db.document_set import fetch_user_document_sets
    from danswer.server.documents.models import ConnectorCredentialPairDescriptor
    from danswer.server.documents.models import ConnectorSnapshot
    from danswer.server.documents.models import CredentialSnapshot

    document_set_info = fetch_user_document_sets(user_id=user_id, db_session=db_session)
    return [
        DocumentSet(
            id=document_set_db_model.id,
            name=document_set_db_model.name,
            description=document_set_db_model.description,
            contains_non_public=any(not cc_pair.is_public for cc_pair in cc_pairs),
            cc_pair_descriptors=[
                ConnectorCredentialPairDescriptor(
                    id=cc_pair.id,
                    name=cc_pair.name,
                    connector=ConnectorSnapshot.from_connector_db_model(
                        cc_pair.connector
                    ),
                    credential=CredentialSnapshot.from_credential_db_model(
                        cc_pair.credential
                    ),
                )
                for cc_pair in cc_pairs
            ],
            is_up_to_date=document_set_db_model.is_up_to_date,
            is_public=document_set_db_model.is_public,
            users=[user.id for user in document_set_db_model.users],
            groups=[group.id for group in document_set_db_model.groups],
        )
        for document_set_db_model, cc_pairs in document_set_info
    ]


# ---- Fail-open Redis helpers (mirror persona_cache.py) ----


def _safe_get(key: str) -> tuple[bool, Any]:
    """Return ``(hit, value)``; ``hit=False`` covers miss AND any Redis or
    decode error — the caller treats them all as "go to the DB"."""
    try:
        # decode_responses=False on the pool → bytes | None. The cast just
        # collapses redis-py's sync/async overload union for mypy.
        raw = cast("bytes | None", get_redis_client().get(key))
    except Exception as e:
        logger.warning("document_set_cache: Redis GET failed for %s: %s", key, e)
        return (False, None)
    if raw is None:
        return (False, None)
    try:
        return (True, json.loads(raw))
    except (TypeError, ValueError) as e:
        logger.warning("document_set_cache: corrupt entry at %s, ignoring: %s", key, e)
        return (False, None)


def _safe_set(key: str, val: Any) -> None:
    try:
        payload = json.dumps(val)
    except (TypeError, ValueError) as e:
        logger.warning("document_set_cache: skipping non-JSON value at %s: %s", key, e)
        return
    try:
        get_redis_client().set(key, payload, ex=DOCUMENT_SET_CACHE_TTL_SECONDS)
    except Exception as e:
        logger.warning("document_set_cache: Redis SET failed for %s: %s", key, e)
