"""kNN-over-Slack assistant router — the fallback when `keyword_route` misses.

Replaces the LLM routing-instructions logic. Routes by nearest-neighbor over the
EXISTING Vespa slack corpus (thread-start chunks, `chunk_id=0`), labeled by
`channel -> persona` via `slack_bot_config`. Self-maintaining: there are no
per-assistant routing instructions to curate — new product -> new help channel ->
questions flow in -> the router learns it.

Pipeline:
  embed question
  -> Vespa kNN (source_type=slack, chunk_id=0)
  -> similarity-weighted vote over neighbors' personas, intersected with the
     caller's ACL catalog (so it can never route to an inaccessible assistant)
  -> confidence gate
  -> on LOW confidence, one fast-LLM tiebreak among the neighbors' assistants.
     NOTE: that LLM only ever sees the retrieved neighbor questions — never
     `routing_instructions` — so it is consistent with removing that logic.

Fail-OPEN everywhere: any error / empty result -> RouteResult(persona_id=None),
and the caller falls back to the all-source default persona.

Prototype validation (leave-one-out, prod, n=285/51 channels): vote-only 64.6%
top-1, vote+LLM tiebreak 70.5% top-1, 85.3% top-3.
"""
import json
from collections import defaultdict

import requests
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from danswer.configs.chat_configs import SLACK_KNN_ROUTER_LLM_CONF_THRESHOLD
from danswer.configs.chat_configs import SLACK_KNN_ROUTER_TOP_K
from danswer.configs.constants import CHUNK_ID
from danswer.configs.constants import CONTENT
from danswer.configs.constants import DOCUMENT_ID
from danswer.configs.constants import EMBEDDINGS
from danswer.configs.constants import METADATA
from danswer.configs.constants import SOURCE_TYPE
from danswer.db.embedding_model import get_current_db_embedding_model
from danswer.db.models import SlackBotConfig
from danswer.document_index.vespa.index import SEARCH_ENDPOINT
from danswer.llm.interfaces import LLM
from danswer.llm.utils import message_to_string
from danswer.search.enums import EmbedTextType
from danswer.search.search_nlp_models import EmbeddingModel
from danswer.secondary_llm_flows.assistant_router import RouteResult
from danswer.secondary_llm_flows.assistant_router import RouterCatalogEntry
from danswer.utils.logger import setup_logger
from shared_configs.configs import MODEL_SERVER_HOST
from shared_configs.configs import MODEL_SERVER_PORT

logger = setup_logger()

_VESPA_TIMEOUT = "3s"


class SlackNeighbor(BaseModel):
    channel: str
    score: float
    content: str


def build_channel_persona_map(db_session: Session) -> dict[str, int]:
    """`slack help-channel name -> persona_id`, from every SlackBotConfig.

    Raw map: it may include the generic/Slack-bot personas. The caller intersects
    with its ACL catalog (which already excludes the default 'Darwin' + Slack-bot
    + hidden personas), so no channel needs special-casing here."""
    mapping: dict[str, int] = {}
    for cfg in db_session.execute(select(SlackBotConfig)).scalars():
        if cfg.persona_id is None:
            continue
        for channel in (cfg.channel_config or {}).get("channel_names", []) or []:
            mapping[channel] = cfg.persona_id
    return mapping


def _channel_of(fields: dict) -> str | None:
    """metadata comes back from Vespa as a JSON string (or dict); pull Channel."""
    meta = fields.get(METADATA)
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return None
    return meta.get("Channel") if isinstance(meta, dict) else None


def retrieve_slack_neighbors(
    question: str,
    db_session: Session,
    top_k: int = SLACK_KNN_ROUTER_TOP_K,
) -> list[SlackNeighbor]:
    """Embed the question and kNN it against slack thread-start chunks
    (source_type=slack, chunk_id=0). Fail-OPEN -> [] on any error."""
    if not question.strip():
        return []
    try:
        db_embedding_model = get_current_db_embedding_model(db_session)
        embedder = EmbeddingModel(
            model_name=db_embedding_model.model_name,
            query_prefix=db_embedding_model.query_prefix,
            passage_prefix=db_embedding_model.passage_prefix,
            normalize=db_embedding_model.normalize,
            server_host=MODEL_SERVER_HOST,
            server_port=MODEL_SERVER_PORT,
        )
        embedding = embedder.encode([question], EmbedTextType.QUERY)[0]
        index_name = db_embedding_model.index_name
        yql = (
            f"select {DOCUMENT_ID}, {METADATA}, {CONTENT} from {index_name} where "
            f'{SOURCE_TYPE} contains "slack" and {CHUNK_ID} = 0 and '
            f"(({{targetHits:{10 * top_k}}}nearestNeighbor({EMBEDDINGS}, query_embedding)))"
        )
        resp = requests.post(
            SEARCH_ENDPOINT,
            json={
                "yql": yql,
                "input.query(query_embedding)": str(embedding),
                "ranking.profile": f"hybrid_search{len(embedding)}",
                "hits": top_k,
                "timeout": _VESPA_TIMEOUT,
            },
        )
        resp.raise_for_status()
        hits = resp.json().get("root", {}).get("children", []) or []
    except Exception as e:
        logger.warning("slack-knn router: neighbor retrieval failed: %s", e)
        return []

    neighbors: list[SlackNeighbor] = []
    for hit in hits:
        fields = hit.get("fields", {})
        channel = _channel_of(fields)
        if not channel:
            continue
        neighbors.append(
            SlackNeighbor(
                channel=channel,
                score=float(hit.get("relevance") or 0.0),
                content=(fields.get(CONTENT) or "").replace("\n", " "),
            )
        )
    return neighbors


_TIEBREAK_PROMPT = """\
You route an internal user question to exactly ONE assistant.

USER QUESTION:
"{question}"

Similar past questions and the assistant that answered each:
{examples}

Valid assistants: {names}.
Reply with ONLY the assistant name, exactly as written above."""


def _llm_tiebreak(
    question: str,
    examples: list[tuple[str, str]],  # (assistant_name, neighbor_snippet)
    name_to_id: dict[str, int],
    llm: LLM,
) -> int | None:
    """Ask the fast LLM to pick one assistant among the retrieved neighbors.
    Only sees neighbor questions (never routing_instructions). None on any issue."""
    rendered = "\n".join(
        f'{i}. [{name}] "{snippet[:160]}"'
        for i, (name, snippet) in enumerate(examples[:10], 1)
    )
    prompt = _TIEBREAK_PROMPT.format(
        question=question[:400],
        examples=rendered,
        names=", ".join(name_to_id),
    )
    try:
        out = message_to_string(llm.invoke(prompt)).strip()
    except Exception as e:
        logger.warning("slack-knn router: tiebreak LLM call failed: %s", e)
        return None
    lowered = out.lower()
    for name, pid in name_to_id.items():
        if name.lower() == lowered:
            return pid
    for name, pid in name_to_id.items():
        if name.lower() in lowered:
            return pid
    return None


def knn_route(
    question: str,
    neighbors: list[SlackNeighbor],
    channel_to_persona: dict[str, int],
    catalog: list[RouterCatalogEntry],
    llm: LLM,
    top_n: int = 3,
    conf_threshold: float = SLACK_KNN_ROUTER_LLM_CONF_THRESHOLD,
) -> RouteResult:
    """Vote over the neighbors' personas (kept to the ACL catalog), gate on
    confidence, and LLM-tiebreak the low-confidence cases. Fail-OPEN."""
    catalog_ids = {entry.persona_id for entry in catalog}
    id_to_name = {entry.persona_id: entry.name for entry in catalog}

    votes: dict[int, float] = defaultdict(float)
    examples: list[tuple[str, str]] = []  # (name, snippet) best-first, catalog-only
    for neighbor in neighbors:
        pid = channel_to_persona.get(neighbor.channel)
        if pid is None or pid not in catalog_ids:
            continue
        votes[pid] += neighbor.score
        examples.append((id_to_name[pid], neighbor.content))

    if not votes:
        return RouteResult(persona_id=None, confidence=0.0)

    ranked = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
    ranked_ids = [pid for pid, _ in ranked[:top_n]]
    top_id, top_weight = ranked[0]
    total_weight = sum(votes.values())
    confidence = top_weight / total_weight if total_weight else 0.0

    final_id = top_id
    ambiguous = confidence < conf_threshold and len(votes) > 1
    if ambiguous:
        name_to_id = {id_to_name[pid]: pid for pid in votes}
        picked = _llm_tiebreak(question, examples, name_to_id, llm)
        if picked is not None:
            final_id = picked

    logger.info(
        "slack-knn router: question=%r -> persona_id=%s confidence=%.2f ranked=%s",
        question[:80],
        final_id,
        confidence,
        ranked_ids,
    )
    # Put the final pick first in ranked_ids (drives the "recommended" list) so a
    # tiebreak override is reflected as the #1 recommendation too.
    if final_id in ranked_ids:
        ranked_ids.remove(final_id)
    ranked_ids.insert(0, final_id)
    return RouteResult(
        persona_id=final_id,
        confidence=confidence,
        ranked_ids=ranked_ids[:top_n],
        ambiguous=ambiguous,
    )
