from typing import cast

from danswer.configs.app_configs import CHAT_FILE_MAX_SIZE_MB
from danswer.configs.chat_configs import DISABLE_LLM_CHUNK_FILTER
from danswer.configs.chat_configs import LLM_RELEVANCE_FILTER_ENABLED
from danswer.dynamic_configs.factory import get_dynamic_config_store
from danswer.dynamic_configs.interface import ConfigNotFoundError
from danswer.server.settings.models import Settings
from shared_configs.configs import RERANK_ENABLED


_SETTINGS_KEY = "danswer_settings"


def load_settings() -> Settings:
    dynamic_config_store = get_dynamic_config_store()
    try:
        settings = Settings(**cast(dict, dynamic_config_store.load(_SETTINGS_KEY)))
    except ConfigNotFoundError:
        settings = Settings()
        dynamic_config_store.store(_SETTINGS_KEY, settings.dict())

    # Env-controlled, not admin-stored — always reflect the current env so the
    # chat UI pre-check matches the backend's CHAT_FILE_MAX_SIZE_MB.
    settings.chat_file_max_size_mb = CHAT_FILE_MAX_SIZE_MB

    # Cluster-level enablement of the search-quality features, surfaced so the UI
    # can hide the rerank/relevance toggles when they're disabled cluster-wide.
    # These mirror the backend's own gating exactly (relevance also off when the
    # DISABLE_LLM_CHUNK_FILTER kill-switch is set).
    settings.rerank_enabled = RERANK_ENABLED
    settings.llm_relevance_filter_enabled = (
        LLM_RELEVANCE_FILTER_ENABLED and not DISABLE_LLM_CHUNK_FILTER
    )

    return settings


def store_settings(settings: Settings) -> None:
    get_dynamic_config_store().store(_SETTINGS_KEY, settings.dict())
