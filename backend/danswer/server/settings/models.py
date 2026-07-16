from enum import Enum

from pydantic import BaseModel


class PageType(str, Enum):
    CHAT = "chat"
    SEARCH = "search"


class AutoSearchRollout(str, Enum):
    """Staged rollout state for the auto-routed one-shot Search tab.

    - OFF: nobody (kill-switch).
    - ADMIN_ONLY: admins only — lets admins clean assistant routing metadata and
      test the routed answer before GA. This is the DEFAULT, so a fresh deploy
      lands here automatically (a newly-added Settings field is absent from the
      already-persisted settings dict, so it takes this default).
    - EVERYONE: generally available.

    Flip in Admin -> Settings; no redeploy. Enforced on the trusted side in the
    /search/auto endpoint, not just by hiding the tab.
    """

    OFF = "off"
    ADMIN_ONLY = "admin_only"
    EVERYONE = "everyone"


class Settings(BaseModel):
    """General settings"""

    chat_page_enabled: bool = True
    search_page_enabled: bool = True
    # Staged rollout of the auto-routed Search tab. Default ADMIN_ONLY so the
    # feature ships dark-to-users on deploy; an admin flips it to EVERYONE in
    # Admin -> Settings when ready. See AutoSearchRollout.
    auto_search_rollout: AutoSearchRollout = AutoSearchRollout.ADMIN_ONLY
    # Semantic intent pre-route (the LLM phrase router between keyword and the LLM
    # instruction router). Default False — OFF — so it stays dark until an admin
    # enables it in Admin → Settings; keyword + instruction routing are unaffected.
    auto_search_intent_enabled: bool = False
    # Side-by-side compare on the Search tab: alongside the single top-1 answer,
    # also answer over the UNION of the router's top-N assistants' document sets
    # (answered by AUTO_SEARCH_UNION_LLM_*, default Sonnet). Only runs on LLM-router
    # picks (keyword routes / @mentions stay single-scope). Default True — ON — for
    # now (the comparison/testing phase); an admin flips it in Admin → Settings.
    # NOTE: adds a second full search+answer pass per question, so it roughly
    # doubles latency and cost while on.
    auto_search_compare_enabled: bool = True
    # Fresh installs land on the chat page by default. NOTE: this only seeds
    # deployments with no stored settings yet — once settings are persisted, the
    # stored value wins, so flip it in Admin → Settings on existing deployments.
    default_page: PageType = PageType.CHAT
    # Show the "Create assistant" entry points to end users: the Create buttons on
    # the My Assistants + Assistant Gallery pages and the "Create a new assistant"
    # row in the chat @-mention menu. Default False so creation ships hidden on
    # deploy (a newly-added Settings field is absent from the already-persisted
    # settings dict, so it takes this default); an admin flips it in Admin →
    # Settings. Gates UI only; the /assistants/new route itself still exists.
    enable_assistant_creation: bool = False
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
