from enum import Enum

from pydantic import BaseModel


class PageType(str, Enum):
    CHAT = "chat"
    SEARCH = "search"


class Settings(BaseModel):
    """General settings"""

    chat_page_enabled: bool = True
    search_page_enabled: bool = True
    # Fresh installs land on the chat page by default. NOTE: this only seeds
    # deployments with no stored settings yet — once settings are persisted, the
    # stored value wins, so flip it in Admin → Settings on existing deployments.
    default_page: PageType = PageType.CHAT
    maximum_chat_retention_days: int | None = None
    # Env-driven (CHAT_FILE_MAX_SIZE_MB), injected in load_settings — surfaced
    # here so the chat UI pre-checks against the SAME value the backend enforces
    # instead of a hardcoded duplicate.
    chat_file_max_size_mb: int = 25
    # Env-driven (RERANK_ENABLED / LLM_RELEVANCE_FILTER_ENABLED), injected in
    # load_settings — surfaced so the chat + assistant UIs hide the
    # per-conversation and per-assistant rerank / relevance toggles when the
    # feature is disabled cluster-wide. Effective values (relevance also respects
    # the DISABLE_LLM_CHUNK_FILTER kill-switch).
    rerank_enabled: bool = False
    llm_relevance_filter_enabled: bool = False

    def check_validity(self) -> None:
        chat_page_enabled = self.chat_page_enabled
        search_page_enabled = self.search_page_enabled
        default_page = self.default_page

        if chat_page_enabled is False and search_page_enabled is False:
            raise ValueError(
                "One of `search_page_enabled` and `chat_page_enabled` must be True."
            )

        if default_page == PageType.CHAT and chat_page_enabled is False:
            raise ValueError(
                "The default page cannot be 'chat' if the chat page is disabled."
            )

        if default_page == PageType.SEARCH and search_page_enabled is False:
            raise ValueError(
                "The default page cannot be 'search' if the search page is disabled."
            )
