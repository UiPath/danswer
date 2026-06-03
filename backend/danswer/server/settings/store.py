from typing import cast

from danswer.configs.app_configs import CHAT_FILE_MAX_SIZE_MB
from danswer.dynamic_configs.factory import get_dynamic_config_store
from danswer.dynamic_configs.interface import ConfigNotFoundError
from danswer.server.settings.models import Settings


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

    return settings


def store_settings(settings: Settings) -> None:
    get_dynamic_config_store().store(_SETTINGS_KEY, settings.dict())
