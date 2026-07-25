"""Shared assistant-routing decision for the auto-routed Search experience.

Both the web one-shot Search tab (server/query_and_chat/query_backend.py) and the
Slack bot's "Search" mode route a question to the best assistant through the SAME
pipeline here, so the two surfaces stay in lockstep:

  build_router_catalog_for_user  -> ACL-scoped, visible, non-Slack, router-opted-in
  resolve_search_persona         -> keyword_route (deterministic) then kNN-over-Slack
                                     fallback; fail-OPEN to the all-source default.
"""
from pydantic import BaseModel
from sqlalchemy.orm import Session

from danswer.configs.chat_configs import ASSISTANT_ROUTER_LLM_MODEL
from danswer.configs.chat_configs import ASSISTANT_ROUTER_LLM_VENDOR
from danswer.configs.chat_configs import AUTO_SEARCH_TOP_N
from danswer.db.constants import SLACK_BOT_PERSONA_PREFIX
from danswer.db.models import User
from danswer.db.persona_cache import get_personas_for_user_cached
from danswer.llm.factory import get_default_llms
from danswer.llm.factory import get_llm
from danswer.llm.interfaces import LLM
from danswer.secondary_llm_flows.assistant_router import build_router_catalog
from danswer.secondary_llm_flows.assistant_router import keyword_route
from danswer.secondary_llm_flows.assistant_router import RouterCatalogEntry
from danswer.secondary_llm_flows.slack_knn_router import build_channel_persona_map
from danswer.secondary_llm_flows.slack_knn_router import knn_route
from danswer.secondary_llm_flows.slack_knn_router import retrieve_slack_neighbors
from danswer.utils.logger import setup_logger

logger = setup_logger()

# All-source default persona ("Darwin", id 0) — the router's fail-open fallback
# when no assistant clearly fits the question.
DEFAULT_SEARCH_PERSONA_ID = 0


class RouteResolution(BaseModel):
    """Outcome of routing a Search question to an assistant."""

    persona_id: int  # DEFAULT_SEARCH_PERSONA_ID when nothing clearly fit
    confidence: float
    ranked_ids: list[int]  # best-first top-N; [1:] are the recommended assistants
    ambiguous: bool
    routed: bool  # True iff a specific (non-default) assistant was picked


def get_router_llm() -> LLM:
    """The LLM used for assistant routing. When ASSISTANT_ROUTER_LLM_VENDOR +
    ASSISTANT_ROUTER_LLM_MODEL are set (e.g. awsbedrock + Claude Sonnet), route
    with that model for sharper selection; otherwise use the default fast LLM."""
    if ASSISTANT_ROUTER_LLM_VENDOR and ASSISTANT_ROUTER_LLM_MODEL:
        return get_llm(
            provider=ASSISTANT_ROUTER_LLM_VENDOR, model=ASSISTANT_ROUTER_LLM_MODEL
        )
    _, fast_llm = get_default_llms()
    return fast_llm


def build_router_catalog_for_user(
    user: User | None, db_session: Session
) -> list[RouterCatalogEntry]:
    """Build the router catalog from the user's accessible assistants.

    Uses the Redis-backed persona cache (get_personas_for_user_cached), which is
    ACL-filtered AND write-through invalidated on EVERY persona mutation — so the
    catalog bursts and repopulates whenever an admin updates an assistant. Drops
    the default ('Darwin', the fallback), Slack-bot, and hidden personas — none
    are routing targets."""
    snapshots = get_personas_for_user_cached(
        user_id=user.id if user else None, db_session=db_session
    )
    routable = [
        snapshot
        for snapshot in snapshots
        if snapshot.is_visible
        and not snapshot.default_persona
        and not snapshot.name.startswith(SLACK_BOT_PERSONA_PREFIX)
        # Admin opt-out: exclude assistants flagged out of auto-routing.
        and snapshot.is_router_candidate
    ]
    return build_router_catalog(routable)


def resolve_search_persona(
    question: str,
    catalog: list[RouterCatalogEntry],
    router_llm: LLM,
    db_session: Session,
    top_n: int = AUTO_SEARCH_TOP_N,
) -> RouteResolution:
    """Route a Search question to the best assistant, best-first.

      1) keyword_route — deterministic keyword override (no LLM); fires only on a
         configured keyword match, otherwise falls through.
      2) kNN-over-Slack fallback — nearest-neighbor over past help-channel
         questions (channel -> persona), weighted vote + an LLM tiebreak on the
         close calls. Its vote ranking populates ranks 2..N (the recommendations).

    Fail-OPEN: any error -> the all-source default persona (DEFAULT_SEARCH_PERSONA_ID)."""
    target_persona_id = DEFAULT_SEARCH_PERSONA_ID
    confidence = 0.0
    ranked_ids: list[int] = []
    ambiguous = False

    kw = keyword_route(question, catalog)
    if kw is not None and kw.persona_id is not None:
        target_persona_id = kw.persona_id
        confidence = kw.confidence
    else:
        try:
            neighbors = retrieve_slack_neighbors(question, db_session)
            channel_map = build_channel_persona_map(db_session)
            route = knn_route(
                question, neighbors, channel_map, catalog, router_llm, top_n=top_n
            )
            if route.persona_id is not None:
                target_persona_id = route.persona_id
            confidence = route.confidence
            ranked_ids = route.ranked_ids
            ambiguous = route.ambiguous
        except Exception as e:
            logger.warning("Search routing unavailable, using fallback: %s", e)

    return RouteResolution(
        persona_id=target_persona_id,
        confidence=confidence,
        ranked_ids=ranked_ids,
        ambiguous=ambiguous,
        routed=target_persona_id != DEFAULT_SEARCH_PERSONA_ID,
    )
