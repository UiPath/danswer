"""Per-user document-set list cache, Redis-backed.

The chat-page bundle fires ``GET /document-set`` →
``server/features/document_set/api.py::list_document_sets`` →
``db/document_set.py::fetch_user_document_sets(user_id, …)`` on *every*
page load. That read is a multi-join (DocumentSet ⋈ cc-pair mapping ⋈
ConnectorCredentialPair) plus, in EE, a per-user permission query — run
once per user per load. At a few hundred users clicking around chat it
adds avoidable DB-pool pressure.

Why **per-user** and not the global-list + Python-filter shape the
persona cache uses: the document-set permission filter is
edition-dependent. The EE override
(``ee/danswer/db/document_set.py::fetch_document_sets``) filters by
``is_public`` / ``DocumentSet__User`` / ``DocumentSet__UserGroup``, while
the MIT base returns *all* document sets (they're organizational, not
permission-enforced — the documents themselves are enforced at search
time). Replicating that branchy logic in Python risks showing a user doc
sets they shouldn't. Memoizing the exact result the versioned query
returned **for that user** can never leak: it caches precisely what the
real query produced. The trade-off is that a cold burst of N distinct
users still costs N first-loads (not 1) — but repeat loads by the same
user (the common case: new-chat / navigation / ``router.refresh``)
collapse to a single DB hit per TTL window.

**Invalidation:** write-through. Every committing mutation in
``db/document_set.py`` (create / update / sync / delete /
to-be-deleted / get-or-create) calls :func:`invalidate_document_sets_all`
after commit, which drops *all* per-user entries (membership/privacy
changes can affect who sees what, so a targeted bust isn't enough). The
``DOCUMENT_SET_CACHE_TTL_SECONDS`` backstop heals any missed bust;
staleness is cosmetic (names/membership in the UI list), never a
permission boundary.

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


logger = setup_logger()


# Per-user namespace. Any document-set mutation busts every key under it.
_DOC_SETS_USER_KEY_PREFIX = DANSWER_REDIS_KEY_PREFIX + "document_sets:user:"
# Stable suffix for the no-auth (user_id is None) case so it gets its own entry.
_NOAUTH_SUFFIX = "__noauth__"


def _user_key(user_id: UUID | None) -> str:
    return _DOC_SETS_USER_KEY_PREFIX + (
        str(user_id) if user_id is not None else _NOAUTH_SUFFIX
    )


# ---------------------------------------------------------------------------
# Public API — read path
# ---------------------------------------------------------------------------


def get_document_sets_for_user_cached(
    user_id: UUID | None, db_session: Session
) -> list[DocumentSet]:
    """Return the ``DocumentSet`` list visible to ``user_id``.

    Cache hit → deserialize stored JSON back into ``DocumentSet`` models.
    Miss / disabled / Redis error → build from the DB via the versioned
    ``fetch_user_document_sets`` (the source of truth, edition-correct).
    """
    if not DOCUMENT_SET_CACHE_ENABLED:
        return _build_for_user(user_id, db_session)

    key = _user_key(user_id)
    hit, cached = _safe_get(key)
    if hit and isinstance(cached, list):
        try:
            return [DocumentSet.parse_obj(d) for d in cached]
        except Exception as e:
            # Schema drift since the entry was cached — treat as a miss.
            logger.warning(
                "Cached document-set list failed DocumentSet parse, refilling: %s", e
            )

    result = _build_for_user(user_id, db_session)
    _safe_set(key, [json.loads(ds.json()) for ds in result])
    return result


# ---------------------------------------------------------------------------
# Public API — invalidation
# ---------------------------------------------------------------------------


def invalidate_document_sets_all() -> None:
    """Drop every per-user cached document-set list.

    Call *after* ``db_session.commit()`` in any mutation that changes a
    DocumentSet, its cc-pair mapping, or its user/group privacy. A blanket
    bust (not a targeted one) because privacy/membership changes alter
    which users should see a set. Cheap no-op when the cache is disabled.
    """
    if not DOCUMENT_SET_CACHE_ENABLED:
        return
    try:
        client = get_redis_client()
        keys = list(client.scan_iter(match=_DOC_SETS_USER_KEY_PREFIX + "*", count=500))
        if keys:
            client.delete(*keys)
    except Exception as e:
        # Fail-open — the TTL backstop heals it. Loud log for a persistent
        # Redis outage.
        logger.warning("invalidate_document_sets_all: Redis bust failed: %s", e)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_for_user(user_id: UUID | None, db_session: Session) -> list[DocumentSet]:
    """Build the per-user ``DocumentSet`` list exactly as the endpoint did.

    Local imports keep this module free of an import cycle: ``db.document_set``
    imports :func:`invalidate_document_sets_all` from here at module load, so
    we must not import it at module level in return.
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
