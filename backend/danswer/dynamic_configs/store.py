import json
import os
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from filelock import FileLock
from redis import Redis
from redis import RedisError
from sqlalchemy.orm import Session

from danswer.db.engine import SessionFactory
from danswer.db.models import KVStore
from danswer.dynamic_configs.interface import ConfigNotFoundError
from danswer.dynamic_configs.interface import DynamicConfigStore
from danswer.dynamic_configs.interface import JSON_ro
from danswer.redis.redis_pool import DANSWER_REDIS_KEY_PREFIX
from danswer.redis.redis_pool import get_redis_client
from danswer.utils.logger import setup_logger


logger = setup_logger()


FILE_LOCK_TIMEOUT = 10


def _get_file_lock(file_name: Path) -> FileLock:
    return FileLock(file_name.with_suffix(".lock"))


class FileSystemBackedDynamicConfigStore(DynamicConfigStore):
    def __init__(self, dir_path: str) -> None:
        # TODO (chris): maybe require all possible keys to be passed in
        # at app start somehow to prevent key overlaps
        self.dir_path = Path(dir_path)

    def store(self, key: str, val: JSON_ro, encrypt: bool = False) -> None:
        file_path = self.dir_path / key
        lock = _get_file_lock(file_path)
        with lock.acquire(timeout=FILE_LOCK_TIMEOUT):
            with open(file_path, "w+") as f:
                json.dump(val, f)

    def load(self, key: str) -> JSON_ro:
        file_path = self.dir_path / key
        if not file_path.exists():
            raise ConfigNotFoundError
        lock = _get_file_lock(file_path)
        with lock.acquire(timeout=FILE_LOCK_TIMEOUT):
            with open(self.dir_path / key) as f:
                return cast(JSON_ro, json.load(f))

    def delete(self, key: str) -> None:
        file_path = self.dir_path / key
        if not file_path.exists():
            raise ConfigNotFoundError
        lock = _get_file_lock(file_path)
        with lock.acquire(timeout=FILE_LOCK_TIMEOUT):
            os.remove(file_path)


class PostgresBackedDynamicConfigStore(DynamicConfigStore):
    @contextmanager
    def get_session(self) -> Iterator[Session]:
        session: Session = SessionFactory()
        try:
            yield session
        finally:
            session.close()

    def store(self, key: str, val: JSON_ro, encrypt: bool = False) -> None:
        # The actual encryption/decryption is done in Postgres, we just need to choose
        # which field to set
        encrypted_val = val if encrypt else None
        plain_val = val if not encrypt else None
        with self.get_session() as session:
            obj = session.query(KVStore).filter_by(key=key).first()
            if obj:
                obj.value = plain_val
                obj.encrypted_value = encrypted_val
            else:
                obj = KVStore(
                    key=key, value=plain_val, encrypted_value=encrypted_val
                )  # type: ignore
                session.query(KVStore).filter_by(key=key).delete()  # just in case
                session.add(obj)
            session.commit()

    def load(self, key: str) -> JSON_ro:
        with self.get_session() as session:
            obj = session.query(KVStore).filter_by(key=key).first()
            if not obj:
                raise ConfigNotFoundError

            if obj.value is not None:
                return cast(JSON_ro, obj.value)
            if obj.encrypted_value is not None:
                return cast(JSON_ro, obj.encrypted_value)

            return None

    def delete(self, key: str) -> None:
        with self.get_session() as session:
            result = session.query(KVStore).filter_by(key=key).delete()  # type: ignore
            if result == 0:
                raise ConfigNotFoundError
            session.commit()


class RedisCachedDynamicConfigStore(DynamicConfigStore):
    """Read-through / write-through Redis cache over an inner ``DynamicConfigStore``.

    Mirrors the shape of upstream Onyx's ``PgRedisKVStore`` but composed
    via wrapping rather than inheritance so the inner store stays
    single-purpose and the cache layer can wrap *any* future backend.

    Semantics:
      * ``load``: probe Redis; on hit, return; on miss, read inner and
        repopulate Redis (with TTL). Encrypted entries are never cached
        plaintext — they always fall through to the inner store.
      * ``store``: write the inner store first (source of truth), then
        refresh Redis (or invalidate if ``encrypt=True``). The
        inner-first order means a Redis success after an inner failure
        cannot leave Redis holding a value the source of truth lacks.
      * ``delete``: delete inner first, then Redis. Same ordering reason.

    Fail-open: every Redis operation is wrapped — a Redis outage degrades
    latency, not availability. Wrap-then-log-then-fall-through is the
    rule throughout.

    Single-tenant: keys are namespaced by :data:`_KEY_PREFIX` only (no
    tenant id), reflecting this fork's divergence from upstream.
    """

    _KEY_PREFIX = DANSWER_REDIS_KEY_PREFIX + "kv:"

    def __init__(
        self,
        inner: DynamicConfigStore,
        ttl_seconds: int,
        client_factory: Callable[[], Redis] | None = None,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        # Indirection lets tests inject a fake client without monkey-
        # patching the global pool. Production wiring uses the default.
        self._client_factory = client_factory or get_redis_client

    # ---- DynamicConfigStore surface ----

    def store(self, key: str, val: JSON_ro, encrypt: bool = False) -> None:
        self._inner.store(key, val, encrypt=encrypt)
        if encrypt:
            # Never hold plaintext of an encrypted value in Redis — that
            # would silently defeat the encryption-at-rest guarantee.
            # Also invalidates any stale plaintext entry from before
            # the value was switched to encrypted.
            self._safe_redis_delete(key)
            return
        self._safe_redis_set(key, val)

    def load(self, key: str) -> JSON_ro:
        hit, cached = self._safe_redis_get(key)
        if hit:
            return cached
        # May raise ConfigNotFoundError — propagate without caching the miss.
        # (Negative caching has its own correctness traps; skip for now.)
        val = self._inner.load(key)
        self._safe_redis_set(key, val)
        return val

    def delete(self, key: str) -> None:
        self._inner.delete(key)
        self._safe_redis_delete(key)

    # ---- private Redis helpers (all fail-open) ----

    def _redis_key(self, key: str) -> str:
        return self._KEY_PREFIX + key

    def _safe_redis_get(self, key: str) -> tuple[bool, JSON_ro]:
        """Return ``(hit, value)``. ``hit=False`` means cache miss OR
        Redis error — caller treats both the same (read inner).
        """
        try:
            raw = self._client_factory().get(self._redis_key(key))
        except RedisError as e:
            logger.warning("Redis GET failed for kv key=%s: %s", key, e)
            return (False, None)
        if raw is None:
            return (False, None)
        try:
            return (True, cast(JSON_ro, json.loads(raw)))
        except (TypeError, ValueError) as e:
            # Corrupt or legacy-format entry — treat as a miss so the
            # next read repopulates from the inner store.
            logger.warning("Corrupt Redis kv entry for key=%s, ignoring: %s", key, e)
            return (False, None)

    def _safe_redis_set(self, key: str, val: JSON_ro) -> None:
        try:
            payload = json.dumps(val)
        except (TypeError, ValueError) as e:
            # Caller stored a value the inner store accepts but JSON
            # doesn't — log and skip the cache (inner still holds truth).
            logger.warning(
                "Skipping Redis cache for non-JSON-serialisable key=%s: %s", key, e
            )
            return
        try:
            self._client_factory().set(
                self._redis_key(key),
                payload,
                ex=self._ttl,
            )
        except RedisError as e:
            logger.warning("Redis SET failed for kv key=%s: %s", key, e)

    def _safe_redis_delete(self, key: str) -> None:
        try:
            self._client_factory().delete(self._redis_key(key))
        except RedisError as e:
            logger.warning("Redis DEL failed for kv key=%s: %s", key, e)
