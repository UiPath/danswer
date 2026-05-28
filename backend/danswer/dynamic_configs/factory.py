from danswer.configs.app_configs import DYNAMIC_CONFIG_STORE
from danswer.configs.app_configs import REDIS_KV_CACHE_ENABLED
from danswer.configs.app_configs import REDIS_KV_CACHE_TTL_SECONDS
from danswer.dynamic_configs.interface import DynamicConfigStore
from danswer.dynamic_configs.store import FileSystemBackedDynamicConfigStore
from danswer.dynamic_configs.store import PostgresBackedDynamicConfigStore
from danswer.dynamic_configs.store import RedisCachedDynamicConfigStore


def get_dynamic_config_store() -> DynamicConfigStore:
    """Resolve the configured KV store.

    The Postgres-backed store is the source of truth. When
    ``REDIS_KV_CACHE_ENABLED`` is true, we transparently wrap it with a
    read-through / write-through Redis cache — call sites are unchanged.
    Wrapping is additive on top of the configured backend rather than a
    distinct ``DYNAMIC_CONFIG_STORE`` value so "which backend" and "do I
    cache" are independently controllable.
    """
    dynamic_config_store_type = DYNAMIC_CONFIG_STORE
    if dynamic_config_store_type == FileSystemBackedDynamicConfigStore.__name__:
        raise NotImplementedError("File based config store no longer supported")
    if dynamic_config_store_type == PostgresBackedDynamicConfigStore.__name__:
        inner: DynamicConfigStore = PostgresBackedDynamicConfigStore()
        if REDIS_KV_CACHE_ENABLED:
            return RedisCachedDynamicConfigStore(
                inner=inner,
                ttl_seconds=REDIS_KV_CACHE_TTL_SECONDS,
            )
        return inner

    # TODO: change exception type
    raise Exception("Unknown dynamic config store type")
