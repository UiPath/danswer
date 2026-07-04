from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from danswer.auth.api_key import validate_api_key
from danswer.auth.users import current_admin_user
from danswer.auth.users import current_user
from danswer.configs.constants import DocumentSource
from danswer.db.embedding_model import get_current_db_embedding_model
from danswer.db.engine import get_session
from danswer.db.models import Persona
from danswer.db.models import User
from danswer.db.tag import get_tags_by_value_prefix_for_source_types
from danswer.document_index.factory import get_default_document_index
from danswer.document_index.vespa.index import VespaIndex
from danswer.auth.schemas import UserRole
from danswer.configs.chat_configs import ASSISTANT_ROUTER_LLM_MODEL
from danswer.configs.chat_configs import ASSISTANT_ROUTER_LLM_VENDOR
from danswer.configs.chat_configs import AUTO_SEARCH_TOP_N
from danswer.configs.chat_configs import AUTO_SEARCH_INTENT_THRESHOLD
from danswer.configs.chat_configs import AUTO_SEARCH_UNION_PERSONA_ID
from danswer.configs.chat_configs import AUTO_SEARCH_UNION_LLM_VENDOR
from danswer.configs.chat_configs import AUTO_SEARCH_UNION_LLM_MODEL
from danswer.configs.chat_configs import AUTO_SEARCH_DEFAULT_LLM_VENDOR
from danswer.configs.chat_configs import AUTO_SEARCH_DEFAULT_LLM_MODEL
from danswer.llm.override_models import LLMOverride
from danswer.search.models import BaseFilters
from danswer.configs.constants import MessageType
from danswer.db.constants import SLACK_BOT_PERSONA_PREFIX
from danswer.db.persona import get_persona_by_id
from danswer.db.persona_cache import get_personas_for_user_cached
from danswer.llm.factory import get_default_llms
from danswer.llm.factory import get_llm
from danswer.llm.interfaces import LLM
from danswer.one_shot_answer.answer_question import get_search_answer
from danswer.one_shot_answer.answer_question import stream_search_answer
from danswer.one_shot_answer.models import DirectQARequest
from danswer.one_shot_answer.models import ThreadMessage
from danswer.search.models import OptionalSearchSetting
from danswer.search.models import RetrievalDetails
from danswer.secondary_llm_flows.assistant_router import build_router_catalog
from danswer.secondary_llm_flows.assistant_router import intent_route
from danswer.secondary_llm_flows.assistant_router import keyword_route
from danswer.secondary_llm_flows.assistant_router import route_question
from danswer.server.query_and_chat.models import AnsweredByAssistant
from danswer.server.query_and_chat.models import AutoSearchRequest
from danswer.server.query_and_chat.models import AutoSearchResponse
from danswer.server.query_and_chat.models import AutoSearchUnionRequest
from danswer.server.query_and_chat.models import AutoSearchUnionResponse
from danswer.server.query_and_chat.models import SearchedAssistant
from danswer.server.settings.models import AutoSearchRollout
from danswer.server.settings.store import load_settings
from danswer.search.models import IndexFilters
from danswer.search.models import SearchDoc
from danswer.search.preprocessing.access_filters import build_access_filters_for_user
from danswer.search.preprocessing.danswer_helper import recommend_search_flow
from danswer.search.utils import chunks_or_sections_to_search_docs
from danswer.secondary_llm_flows.query_validation import get_query_answerability
from danswer.secondary_llm_flows.query_validation import stream_query_answerability
from danswer.server.middleware.request_rate_limit import (
    check_message_request_rate_limit,
)
from danswer.server.query_and_chat.models import AdminSearchRequest
from danswer.server.query_and_chat.models import AdminSearchResponse
from danswer.server.query_and_chat.models import HelperResponse
from danswer.server.query_and_chat.models import QueryValidationResponse
from danswer.server.query_and_chat.models import SimpleQueryRequest
from danswer.server.query_and_chat.models import SourceTag
from danswer.server.query_and_chat.models import TagResponse
from danswer.server.query_and_chat.token_limit import check_token_rate_limits
from danswer.utils.logger import setup_logger

logger = setup_logger()

admin_router = APIRouter(prefix="/admin", dependencies=[Depends(validate_api_key)])
basic_router = APIRouter(prefix="/query", dependencies=[Depends(validate_api_key)])


@admin_router.post("/search")
def admin_search(
    question: AdminSearchRequest,
    user: User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> AdminSearchResponse:
    query = question.query
    logger.info(f"Received admin search query: {query}")

    user_acl_filters = build_access_filters_for_user(user, db_session)
    final_filters = IndexFilters(
        source_type=question.filters.source_type,
        document_set=question.filters.document_set,
        time_cutoff=question.filters.time_cutoff,
        tags=question.filters.tags,
        access_control_list=user_acl_filters,
    )

    embedding_model = get_current_db_embedding_model(db_session)

    document_index = get_default_document_index(
        primary_index_name=embedding_model.index_name, secondary_index_name=None
    )

    if not isinstance(document_index, VespaIndex):
        raise HTTPException(
            status_code=400,
            detail="Cannot use admin-search when using a non-Vespa document index",
        )

    matching_chunks = document_index.admin_retrieval(query=query, filters=final_filters)

    documents = chunks_or_sections_to_search_docs(matching_chunks)

    # Deduplicate documents by id
    deduplicated_documents: list[SearchDoc] = []
    seen_documents: set[str] = set()
    for document in documents:
        if document.document_id not in seen_documents:
            deduplicated_documents.append(document)
            seen_documents.add(document.document_id)
    return AdminSearchResponse(documents=deduplicated_documents)


@basic_router.get("/valid-tags")
def get_tags(
    match_pattern: str | None = None,
    # If this is empty or None, then tags for all sources are considered
    sources: list[DocumentSource] | None = None,
    allow_prefix: bool = True,  # This is currently the only option
    # Optional cap on tags returned. Default None preserves the existing
    # unbounded behavior; a client can pass a limit to bound the response.
    limit: int | None = None,
    _: User = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> TagResponse:
    if not allow_prefix:
        raise NotImplementedError("Cannot disable prefix match for now")

    db_tags = get_tags_by_value_prefix_for_source_types(
        tag_value_prefix=match_pattern,
        sources=sources,
        limit=limit,
        db_session=db_session,
    )
    server_tags = [
        SourceTag(
            tag_key=db_tag.tag_key, tag_value=db_tag.tag_value, source=db_tag.source
        )
        for db_tag in db_tags
    ]
    return TagResponse(tags=server_tags)


@basic_router.post("/search-intent")
def get_search_type(
    simple_query: SimpleQueryRequest,
    _: User = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> HelperResponse:
    logger.info(f"Calculating intent for {simple_query.query}")
    embedding_model = get_current_db_embedding_model(db_session)
    return recommend_search_flow(
        simple_query.query, model_name=embedding_model.model_name
    )


@basic_router.post("/query-validation")
def query_validation(
    simple_query: SimpleQueryRequest, _: User = Depends(current_user)
) -> QueryValidationResponse:
    # Note if weak model prompt is chosen, this check does not occur and will simply return that
    # the query is valid, this is because weaker models cannot really handle this task well.
    # Additionally, some weak model servers cannot handle concurrent inferences.
    logger.info(f"Validating query: {simple_query.query}")
    reasoning, answerable = get_query_answerability(simple_query.query)
    return QueryValidationResponse(reasoning=reasoning, answerable=answerable)


@basic_router.post("/stream-query-validation")
def stream_query_validation(
    simple_query: SimpleQueryRequest, _: User = Depends(current_user)
) -> StreamingResponse:
    # Note if weak model prompt is chosen, this check does not occur and will simply return that
    # the query is valid, this is because weaker models cannot really handle this task well.
    # Additionally, some weak model servers cannot handle concurrent inferences.
    logger.info(f"Validating query: {simple_query.query}")
    return StreamingResponse(
        stream_query_answerability(simple_query.query), media_type="application/json"
    )


@basic_router.post("/stream-answer-with-quote")
def get_answer_with_quote(
    query_request: DirectQARequest,
    request: Request,
    user: User = Depends(current_user),
    # Mirrors /chat/send-message: request-rate cap first (cheap when
    # off), token-budget check second.
    _rate_limit: None = Depends(check_message_request_rate_limit),
    _: None = Depends(check_token_rate_limits),
) -> StreamingResponse:
    query = query_request.messages[0].message
    logger.info(f"Received query for one shot answer with quotes: {query}")
    packets = stream_search_answer(
        query_req=query_request,
        user=user,
        max_document_tokens=None,
        max_history_tokens=0,
    )
    return StreamingResponse(packets, media_type="application/json")


# All-source default persona ("Darwin", id 0) — the router's fail-open fallback
# when no assistant clearly fits the question.
DEFAULT_SEARCH_PERSONA_ID = 0


def _auto_search_allowed(rollout: AutoSearchRollout, user: User | None) -> bool:
    """Trusted-side rollout gate for the auto-routed Search tab. OFF blocks
    everyone; EVERYONE allows all; ADMIN_ONLY allows admins (and the no-auth
    superuser context where user is None, mirroring get_personas' convention)."""
    if rollout == AutoSearchRollout.OFF:
        return False
    if rollout == AutoSearchRollout.EVERYONE:
        return True
    # ADMIN_ONLY
    if user is None:
        return True
    return user.role == UserRole.ADMIN


def _get_router_llm() -> LLM:
    """The LLM used for assistant routing. When ASSISTANT_ROUTER_LLM_VENDOR +
    ASSISTANT_ROUTER_LLM_MODEL are set (e.g. awsbedrock + Claude Sonnet), route
    with that model for sharper selection; otherwise use the default fast LLM."""
    if ASSISTANT_ROUTER_LLM_VENDOR and ASSISTANT_ROUTER_LLM_MODEL:
        return get_llm(
            provider=ASSISTANT_ROUTER_LLM_VENDOR, model=ASSISTANT_ROUTER_LLM_MODEL
        )
    _, fast_llm = get_default_llms()
    return fast_llm


def _get_router_catalog(user: User | None, db_session: Session) -> list:
    """Build the router catalog from the user's accessible assistants.

    Uses the Redis-backed persona cache (get_personas_for_user_cached), which is
    ACL-filtered AND write-through invalidated on EVERY persona mutation (see
    invalidate_personas_all() calls in db/persona.py) — so the catalog bursts and
    repopulates whenever an admin updates an assistant, cross-worker, not just on
    a TTL. When PERSONA_CACHE_ENABLED is false it falls back to a direct DB read.
    We then drop the default ('Darwin', the fallback), Slack-bot, and hidden
    personas — none are routing targets."""
    snapshots = get_personas_for_user_cached(
        user_id=user.id if user else None, db_session=db_session
    )
    routable = [
        snapshot
        for snapshot in snapshots
        if snapshot.is_visible
        and not snapshot.default_persona
        and not snapshot.name.startswith(SLACK_BOT_PERSONA_PREFIX)
    ]
    return build_router_catalog(routable)


@basic_router.post("/auto-search")
def auto_search(
    auto_search_request: AutoSearchRequest,
    request: Request,
    user: User = Depends(current_user),
    db_session: Session = Depends(get_session),
    # Mirrors /chat/send-message + /stream-answer-with-quote: request-rate cap
    # first (cheap when off), token-budget check second.
    _rate_limit: None = Depends(check_message_request_rate_limit),
    _: None = Depends(check_token_rate_limits),
) -> AutoSearchResponse:
    """Auto-route a point-in-time question to the best assistant, then answer it
    with the existing one-shot engine. Picks the assistant via the LLM router
    over the user's ACL-filtered, visible assistants; falls back to the
    all-source default persona when no assistant clearly fits."""
    settings = load_settings()
    # Trusted-side rollout gate — never rely on the FE hiding the tab.
    if not _auto_search_allowed(settings.auto_search_rollout, user):
        raise HTTPException(
            status_code=403,
            detail="Auto-search is not enabled for your account.",
        )

    question = auto_search_request.message
    logger.info(f"Auto-search question: {question}")

    routed_confidence = 0.0
    # The router's best-first top-N ranking. The #1 answers the question (single
    # scope); the rest become "recommended assistants" the user can chat with next.
    # Empty for keyword routes and @mentions (nothing to recommend).
    ranked_ids: list[int] = []
    if auto_search_request.persona_id is not None:
        # User explicitly @mentioned an assistant — skip the LLM router and
        # invoke it directly. Still ACL-re-checked below via get_persona_by_id.
        target_persona_id = auto_search_request.persona_id
        routed_confidence = 1.0
    else:
        # Auto-route. Catalog = the user's accessible, VISIBLE, non-Slack
        # assistants via the Redis persona cache (busted on every assistant
        # mutation). Fail-open: any LLM/availability issue -> all-source fallback.
        catalog = _get_router_catalog(user, db_session)
        target_persona_id = DEFAULT_SEARCH_PERSONA_ID
        # 1) Deterministic keyword override (no LLM call) — additive: only fires
        #    when a configured keyword matches; otherwise falls through to the LLM.
        kw = keyword_route(question, catalog)
        # Semantic intent pre-route is OFF unless an admin enables it (default off).
        intent = (
            intent_route(
                question,
                catalog,
                _get_router_llm(),
                min_confidence=AUTO_SEARCH_INTENT_THRESHOLD,
            )
            if kw is None and settings.auto_search_intent_enabled
            else None
        )
        if kw is not None and kw.persona_id is not None:
            target_persona_id = kw.persona_id
            routed_confidence = kw.confidence
        elif intent is not None and intent.persona_id is not None:
            # 2) Semantic intent pre-route: an LLM matches the question against all
            #    assistants' intent phrases (high-confidence gate). Single scope
            #    like the keyword route; no recommendations. On no confident match
            #    it returns None and we fall through to the instruction router.
            target_persona_id = intent.persona_id
            routed_confidence = intent.confidence
        else:
            # 3) LLM router — returns a best-first top-N ranking in one call. The #1
            #    answers (single scope); ranks 2..N become the recommendations.
            try:
                route = route_question(
                    question, catalog, _get_router_llm(), top_n=AUTO_SEARCH_TOP_N
                )
                if route.persona_id is not None:
                    target_persona_id = route.persona_id
                routed_confidence = route.confidence
                ranked_ids = route.ranked_ids
            except Exception as e:
                logger.warning(
                    "Auto-search routing unavailable, using fallback: %s", e
                )

    def _resolve(pid: int) -> Persona | None:
        """ACL-checked persona fetch; None if inaccessible/missing."""
        try:
            return get_persona_by_id(
                pid, user=user, db_session=db_session, is_for_edit=False
            )
        except Exception:
            return None

    # SINGLE-SCOPE: answer with the ONE routed assistant (its own document sets
    # fence retrieval). On any issue (incl. an explicit persona_id the user can't
    # access) fall back to the all-source default persona so the box never dead-ends.
    persona = _resolve(target_persona_id) or get_persona_by_id(
        DEFAULT_SEARCH_PERSONA_ID, user=user, db_session=db_session, is_for_edit=False
    )
    was_routed = persona.id != DEFAULT_SEARCH_PERSONA_ID
    prompt_id = persona.prompts[0].id if persona.prompts else 0

    # Recommended assistants: the router's next-best picks (ranks 2..N), which the
    # user can click to chat further if #1 wasn't right. Only populated on the LLM
    # route (ranked_ids is empty for keyword routes / @mentions). No extra LLM call.
    other_recommended: list[SearchedAssistant] = []
    for alt_id in ranked_ids:
        if alt_id == persona.id or len(other_recommended) >= 2:
            continue
        alt = _resolve(alt_id)
        if alt is None:
            continue
        other_recommended.append(
            SearchedAssistant(
                persona_id=alt.id, name=alt.name, display_name=alt.display_name
            )
        )

    # Answer via the existing one-shot engine. Passing the authenticated `user`
    # persists the Q + A under their id (one_shot=True) so we capture who-asked-
    # what for quality analysis; the returned chat_message_id powers 👍/👎 + text
    # feedback via the existing /chat/create-chat-message-feedback endpoint.
    # Optional model override for the top-1 answer (default: the persona's own
    # model). Configurable so admins can A/B the default answerer.
    default_llm_override = (
        LLMOverride(
            model_provider=AUTO_SEARCH_DEFAULT_LLM_VENDOR,
            model_version=AUTO_SEARCH_DEFAULT_LLM_MODEL,
        )
        if AUTO_SEARCH_DEFAULT_LLM_VENDOR and AUTO_SEARCH_DEFAULT_LLM_MODEL
        else None
    )
    qa_response = get_search_answer(
        query_req=DirectQARequest(
            messages=[
                ThreadMessage(message=question, sender=None, role=MessageType.USER)
            ],
            prompt_id=prompt_id,
            persona_id=persona.id,
            retrieval_options=RetrievalDetails(
                run_search=OptionalSearchSetting.ALWAYS, real_time=False
            ),
        ),
        user=user,
        max_document_tokens=None,
        max_history_tokens=0,
        db_session=db_session,
        use_citations=True,
        llm_override=default_llm_override,
    )

    # SIDE-BY-SIDE COMPARE (metadata only): mark that a union answer is available
    # and list the assistants it should span (the router's top-N). The union answer
    # itself is produced by /query/auto-search/union, lazy-fetched by the UI AFTER
    # this response — so the top-1 answer is never held up by a second generation.
    # Only offered on LLM-router picks (ranked_ids populated); keyword routes /
    # @mentions stay single-answer.
    compare_enabled = False
    union_assistants: list[SearchedAssistant] = []
    if settings.auto_search_compare_enabled and ranked_ids:
        for pid in ranked_ids[:AUTO_SEARCH_TOP_N]:
            alt = _resolve(pid)
            if alt is not None:
                union_assistants.append(
                    SearchedAssistant(
                        persona_id=alt.id, name=alt.name, display_name=alt.display_name
                    )
                )
        compare_enabled = len(union_assistants) > 0

    return AutoSearchResponse(
        answer=qa_response.answer,
        citations=qa_response.citations,
        docs=qa_response.docs,
        chat_message_id=qa_response.chat_message_id,
        error_msg=qa_response.error_msg,
        other_recommended=other_recommended,
        answered_by=AnsweredByAssistant(
            persona_id=persona.id,
            name=persona.name,
            display_name=persona.display_name,
            routed=was_routed,
            confidence=routed_confidence,
        ),
        compare_enabled=compare_enabled,
        union_assistants=union_assistants,
    )


@basic_router.post("/auto-search/union")
def auto_search_union(
    union_request: AutoSearchUnionRequest,
    request: Request,
    user: User = Depends(current_user),
    db_session: Session = Depends(get_session),
    _rate_limit: None = Depends(check_message_request_rate_limit),
    _: None = Depends(check_token_rate_limits),
) -> AutoSearchUnionResponse:
    """Second (compare) answer for the Search tab: answer the question over the UNION
    of the given assistants' document sets, using the compare model (default Sonnet).
    Lazy-fetched by the UI after the primary answer arrives, so neither answer blocks
    the other. The caller passes the persona ids from AutoSearchResponse.union_assistants
    (no re-routing here — the union scope stays consistent with the primary response)."""
    settings = load_settings()
    if not _auto_search_allowed(settings.auto_search_rollout, user):
        raise HTTPException(
            status_code=403, detail="Auto-search is not enabled for your account."
        )
    if not settings.auto_search_compare_enabled:
        raise HTTPException(status_code=403, detail="Compare answers are disabled.")

    question = union_request.message

    # Union the requested assistants' document sets (ACL-checked fetch; skip any the
    # user can't access). The union persona is fence-less, so this set IS the scope.
    union_set_names: set[str] = set()
    resolved = 0
    for pid in union_request.persona_ids:
        try:
            alt = get_persona_by_id(
                pid, user=user, db_session=db_session, is_for_edit=False
            )
        except Exception:
            continue
        resolved += 1
        union_set_names.update(ds.name for ds in alt.document_sets)
    if resolved == 0:
        return AutoSearchUnionResponse(error_msg="No accessible assistants to compare.")

    try:
        union_persona = get_persona_by_id(
            AUTO_SEARCH_UNION_PERSONA_ID, user=user, db_session=db_session, is_for_edit=False
        )
    except Exception:
        union_persona = get_persona_by_id(
            DEFAULT_SEARCH_PERSONA_ID, user=user, db_session=db_session, is_for_edit=False
        )
    union_prompt_id = union_persona.prompts[0].id if union_persona.prompts else 0
    union_override = (
        LLMOverride(
            model_provider=AUTO_SEARCH_UNION_LLM_VENDOR,
            model_version=AUTO_SEARCH_UNION_LLM_MODEL,
        )
        if AUTO_SEARCH_UNION_LLM_VENDOR and AUTO_SEARCH_UNION_LLM_MODEL
        else None
    )
    try:
        union_response = get_search_answer(
            query_req=DirectQARequest(
                messages=[
                    ThreadMessage(message=question, sender=None, role=MessageType.USER)
                ],
                prompt_id=union_prompt_id,
                persona_id=union_persona.id,
                retrieval_options=RetrievalDetails(
                    run_search=OptionalSearchSetting.ALWAYS,
                    real_time=False,
                    filters=BaseFilters(document_set=sorted(union_set_names) or None),
                ),
            ),
            user=user,
            max_document_tokens=None,
            max_history_tokens=0,
            db_session=db_session,
            use_citations=True,
            llm_override=union_override,
        )
    except Exception as e:
        logger.warning("Auto-search union answer failed: %s", e)
        return AutoSearchUnionResponse(error_msg="Could not generate the compare answer.")

    return AutoSearchUnionResponse(
        answer=union_response.answer,
        citations=union_response.citations,
        docs=union_response.docs,
        error_msg=union_response.error_msg,
    )
