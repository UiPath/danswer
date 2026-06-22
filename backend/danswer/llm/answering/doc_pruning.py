import json
import re
from collections import defaultdict
from copy import deepcopy
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from danswer.chat.models import (
    LlmDoc,
)
from danswer.configs.chat_configs import DOCS_VERSION_DEDUP_URL_SUBSTR
from danswer.configs.chat_configs import MAX_PROMPT_DOCS_PER_SOURCE
from danswer.configs.chat_configs import PROTECTED_SOURCES
from danswer.configs.chat_configs import SOURCE_DIVERSITY_RESERVED_SLOTS
from danswer.configs.constants import IGNORE_FOR_QA
from danswer.configs.model_configs import DOC_EMBEDDING_CONTEXT_SIZE
from danswer.db.models import Document
from danswer.llm.answering.models import DocumentPruningConfig
from danswer.llm.answering.models import PromptConfig
from danswer.llm.answering.prompts.citations_prompt import compute_max_document_tokens
from danswer.llm.interfaces import LLMConfig
from danswer.llm.utils import get_default_llm_tokenizer
from danswer.llm.utils import tokenizer_trim_content
from danswer.prompts.prompt_utils import build_doc_context_str
from danswer.search.models import InferenceChunk
from danswer.tools.search.search_utils import llm_doc_to_dict
from danswer.utils.logger import setup_logger


logger = setup_logger()

T = TypeVar("T", bound=LlmDoc | InferenceChunk)

_METADATA_TOKEN_ESTIMATE = 75


class PruningError(Exception):
    pass


def _compute_limit(
    prompt_config: PromptConfig,
    llm_config: LLMConfig,
    question: str,
    max_chunks: int | None,
    max_window_percentage: float | None,
    max_tokens: int | None,
    tool_token_count: int,
) -> int:
    llm_max_document_tokens = compute_max_document_tokens(
        prompt_config=prompt_config,
        llm_config=llm_config,
        tool_token_count=tool_token_count,
        actual_user_input=question,
    )

    window_percentage_based_limit = (
        max_window_percentage * llm_max_document_tokens
        if max_window_percentage
        else None
    )
    chunk_count_based_limit = (
        max_chunks * DOC_EMBEDDING_CONTEXT_SIZE if max_chunks else None
    )

    limit_options = [
        lim
        for lim in [
            window_percentage_based_limit,
            chunk_count_based_limit,
            max_tokens,
            llm_max_document_tokens,
        ]
        if lim
    ]
    return int(min(limit_options))


def reorder_docs(
    docs: list[T],
    doc_relevance_list: list[bool] | None,
) -> list[T]:
    if doc_relevance_list is None:
        return docs

    reordered_docs: list[T] = []
    if doc_relevance_list is not None:
        for selection_target in [True, False]:
            for doc, is_relevant in zip(docs, doc_relevance_list):
                if is_relevant == selection_target:
                    reordered_docs.append(doc)
    return reordered_docs


def ensure_source_diversity(docs: list[T]) -> list[T]:
    """Guarantee that up to SOURCE_DIVERSITY_RESERVED_SLOTS of the highest-ranked
    docs from PROTECTED_SOURCES survive final selection, so curated KB/web
    content isn't crowded out of the prompt by a chatty high-relevance source
    (e.g. Slack). Promotes those protected docs to the front (keeping their
    relative order); everything else keeps its order. No-op when disabled
    (reserved <= 0), when there are no protected sources, or when none are
    present in `docs`.
    """
    if SOURCE_DIVERSITY_RESERVED_SLOTS <= 0 or not PROTECTED_SOURCES:
        return docs

    protected = set(PROTECTED_SOURCES)
    promote_indices: list[int] = []
    for ind, doc in enumerate(docs):
        source = doc.source_type
        source_str = (source.value if hasattr(source, "value") else str(source)).lower()
        if source_str in protected:
            promote_indices.append(ind)
            if len(promote_indices) >= SOURCE_DIVERSITY_RESERVED_SLOTS:
                break

    if not promote_indices:
        return docs

    promote_set = set(promote_indices)
    promoted = [docs[i] for i in promote_indices]
    rest = [doc for i, doc in enumerate(docs) if i not in promote_set]
    return promoted + rest


def cap_docs_per_source(docs: list[T]) -> list[T]:
    """Cap how many docs any single source contributes to the prompt, preserving
    order (so the highest-ranked / source-diversity-promoted docs per source
    survive and the rest are dropped). Prevents a chatty source from monopolizing
    the context — and therefore the citations — when diverse sources are present.
    No-op when disabled (cap <= 0). Run AFTER ensure_source_diversity so promoted
    curated docs are kept.
    """
    cap = MAX_PROMPT_DOCS_PER_SOURCE
    if cap <= 0:
        return docs

    counts: dict[str, int] = defaultdict(int)
    capped: list[T] = []
    for doc in docs:
        source = doc.source_type
        key = (source.value if hasattr(source, "value") else str(source)).lower()
        if counts[key] >= cap:
            continue
        counts[key] += 1
        capped.append(doc)
    return capped


_DOCS_VERSION_SEG_RE = re.compile(r"^(latest|\d+\.\d+)$")


def _docs_page_and_version(url: str | None) -> tuple[str, str] | None:
    """For a versioned documentation URL, return (page_key, version_token) where
    page_key is the URL with the version path-segment stripped, so the SAME page
    across product versions collapses to one key. Returns None for non-docs URLs
    (other sources are left untouched) and for docs URLs with no recognizable
    version segment. Scoped by DOCS_VERSION_DEDUP_URL_SUBSTR (empty = disabled).
    """
    if not url or not DOCS_VERSION_DEDUP_URL_SUBSTR:
        return None
    if DOCS_VERSION_DEDUP_URL_SUBSTR not in url:
        return None
    parts = url.split("/")
    for i, seg in enumerate(parts):
        if _DOCS_VERSION_SEG_RE.match(seg):
            page_key = "/".join(parts[:i] + parts[i + 1 :])
            return page_key, seg
    return None


def _versioned_url_parts(url: str | None) -> tuple[str, str, str] | None:
    """(prefix, version, suffix) for a versioned docs URL, else None. The page is
    identified by prefix + suffix (version segment stripped); any version's URL is
    rebuilt as f"{prefix}/{version}/{suffix}". Scoped by DOCS_VERSION_DEDUP_URL_SUBSTR."""
    if not url or not DOCS_VERSION_DEDUP_URL_SUBSTR:
        return None
    if DOCS_VERSION_DEDUP_URL_SUBSTR not in url:
        return None
    parts = url.split("/")
    for i, seg in enumerate(parts):
        if _DOCS_VERSION_SEG_RE.match(seg):
            return "/".join(parts[:i]), seg, "/".join(parts[i + 1 :])
    return None


def rewrite_docs_links_to_latest(docs: list[LlmDoc], db_session: Session) -> None:
    """Rewrite each versioned docs link to the NEWEST version of that SAME page that
    exists in the index (same URL with the version path-segment stripped; slug kept).

    Query-time fix for retrieval surfacing a stale version when newer ones are
    indexed: the version dedup only collapses versions that were *retrieved*, so a
    page whose only retrieved chunk is an old version stays old. Here we look up the
    newest indexed version of that page and rewrite the link to it. Mutates `docs`
    in place; no-op for non-docs links and when no newer indexed version exists.
    """
    pages: set[tuple[str, str]] = set()
    for doc in docs:
        pv = _versioned_url_parts(doc.link)
        if pv:
            pages.add((pv[0], pv[2]))
    if not pages:
        return

    # Newest indexed version per (prefix, suffix) page. One prefix-scan per distinct
    # prefix (PK-indexed LIKE 'prefix%'), then match the exact page in Python.
    latest: dict[tuple[str, str], tuple[str, str]] = {}
    for prefix in {p for p, _ in pages}:
        rows = db_session.execute(
            select(Document.id).where(Document.id.like(f"{prefix}/%"))
        ).all()
        for (doc_id,) in rows:
            pv = _versioned_url_parts(doc_id)
            if pv is None:
                continue
            key = (pv[0], pv[2])
            if key not in pages:
                continue
            cur = latest.get(key)
            if cur is None or _docs_version_sort_key(pv[1]) > _docs_version_sort_key(
                cur[0]
            ):
                latest[key] = (pv[1], doc_id)

    for doc in docs:
        pv = _versioned_url_parts(doc.link)
        if pv is None:
            continue
        best = latest.get((pv[0], pv[2]))
        if best and _docs_version_sort_key(best[0]) > _docs_version_sort_key(pv[1]):
            doc.link = best[1]


def _docs_version_sort_key(token: str) -> tuple[int, int, int]:
    """Order doc versions newest-first. Tiered so we never have to decode the
    exact meaning of the post-migration 'N.YYMM' scheme — we only need it to
    outrank the frozen old 'YYYY.M' scheme, which always holds once a product has
    migrated:
        tier 3: 'latest' alias        (always newest)
        tier 2: new scheme  N.YYMM    e.g. 2.2510  (current)
        tier 1: old scheme  YYYY.M    e.g. 2024.10 (frozen)
        tier 0: unrecognized          (sorts last)
    Within a tier, compare the numeric components.
    """
    if token == "latest":
        return (3, 0, 0)
    m = re.match(r"^(\d+)\.(\d+)$", token)
    if not m:
        return (0, 0, 0)
    major, minor = int(m.group(1)), int(m.group(2))
    if major >= 1000:  # YYYY.M (old scheme)
        return (1, major, minor)
    return (2, major, minor)  # N.YYMM (new scheme) -> ranks above all old


def dedupe_doc_versions(
    docs: list[LlmDoc], doc_relevance_list: list[bool] | None
) -> tuple[list[LlmDoc], list[bool] | None]:
    """Collapse the same documentation page repeated across product versions,
    keeping only the newest version's chunk(s) so distinct pages aren't crowded
    out of the LLM context. Only affects versioned docs URLs (see
    DOCS_VERSION_DEDUP_URL_SUBSTR); every other source/doc passes through.
    `doc_relevance_list` is filtered in lockstep (prune_documents requires the
    two stay equal length).
    """
    parsed = [_docs_page_and_version(doc.link or doc.document_id) for doc in docs]

    # Newest version token seen per docs page.
    newest: dict[str, str] = {}
    for pv in parsed:
        if pv is None:
            continue
        page, ver = pv
        if page not in newest or _docs_version_sort_key(
            ver
        ) > _docs_version_sort_key(newest[page]):
            newest[page] = ver

    kept_docs: list[LlmDoc] = []
    kept_rel: list[bool] = []
    dropped = 0
    for i, doc in enumerate(docs):
        pv = parsed[i]
        # Keep non-docs / unversioned docs, and only the newest version per page.
        if pv is None or pv[1] == newest[pv[0]]:
            kept_docs.append(doc)
            if doc_relevance_list is not None:
                kept_rel.append(doc_relevance_list[i])
        else:
            dropped += 1

    if dropped:
        logger.info(
            f"Deduped {dropped} older-version duplicate doc page(s) from the LLM context"
        )
    return kept_docs, (kept_rel if doc_relevance_list is not None else None)


def _remove_docs_to_ignore(docs: list[LlmDoc]) -> list[LlmDoc]:
    return [doc for doc in docs if not doc.metadata.get(IGNORE_FOR_QA)]


def _apply_pruning(
    docs: list[LlmDoc],
    doc_relevance_list: list[bool] | None,
    token_limit: int,
    is_manually_selected_docs: bool,
    use_sections: bool,
    using_tool_message: bool,
) -> list[LlmDoc]:
    llm_tokenizer = get_default_llm_tokenizer()
    docs = deepcopy(docs)  # don't modify in place

    # re-order docs with all the "relevant" docs at the front
    docs = reorder_docs(docs=docs, doc_relevance_list=doc_relevance_list)
    # remove docs that are explicitly marked as not for QA
    docs = _remove_docs_to_ignore(docs=docs)
    # guarantee curated KB/web docs aren't crowded out before the token-budget cut
    docs = ensure_source_diversity(docs)
    # cap any single source so a chatty one can't monopolize the prompt + citations
    docs = cap_docs_per_source(docs)

    tokens_per_doc: list[int] = []
    final_doc_ind = None
    total_tokens = 0
    for ind, llm_doc in enumerate(docs):
        doc_str = (
            json.dumps(llm_doc_to_dict(llm_doc, ind))
            if using_tool_message
            else build_doc_context_str(
                semantic_identifier=llm_doc.semantic_identifier,
                source_type=llm_doc.source_type,
                content=llm_doc.content,
                metadata_dict=llm_doc.metadata,
                updated_at=llm_doc.updated_at,
                ind=ind,
            )
        )

        doc_tokens = len(llm_tokenizer.encode(doc_str))
        # if chunks, truncate chunks that are way too long
        # this can happen if the embedding model tokenizer is different
        # than the LLM tokenizer
        if (
            not is_manually_selected_docs
            and not use_sections
            and doc_tokens > DOC_EMBEDDING_CONTEXT_SIZE + _METADATA_TOKEN_ESTIMATE
        ):
            logger.warning(
                "Found more tokens in chunk than expected, "
                "likely mismatch between embedding and LLM tokenizers. Trimming content..."
            )
            llm_doc.content = tokenizer_trim_content(
                content=llm_doc.content,
                desired_length=DOC_EMBEDDING_CONTEXT_SIZE,
                tokenizer=llm_tokenizer,
            )
            doc_tokens = DOC_EMBEDDING_CONTEXT_SIZE
        tokens_per_doc.append(doc_tokens)
        total_tokens += doc_tokens
        if total_tokens > token_limit:
            final_doc_ind = ind
            break

    if final_doc_ind is not None:
        if is_manually_selected_docs or use_sections:
            # for document selection, only allow the final document to get truncated
            # if more than that, then the user message is too long
            if final_doc_ind != len(docs) - 1:
                if use_sections:
                    # Truncate the rest of the list since we're over the token limit
                    # for the last one, trim it. In this case, the Sections can be rather long
                    # so better to trim the back than throw away the whole thing.
                    docs = docs[: final_doc_ind + 1]
                else:
                    raise PruningError(
                        "LLM context window exceeded. Please de-select some documents or shorten your query."
                    )

            amount_to_truncate = total_tokens - token_limit
            # NOTE: need to recalculate the length here, since the previous calculation included
            # overhead from JSON-fying the doc / the metadata
            final_doc_content_length = len(
                llm_tokenizer.encode(docs[final_doc_ind].content)
            ) - (amount_to_truncate)
            # this could occur if we only have space for the title / metadata
            # not ideal, but it's the most reasonable thing to do
            # NOTE: the frontend prevents documents from being selected if
            # less than 75 tokens are available to try and avoid this situation
            # from occurring in the first place
            if final_doc_content_length <= 0:
                logger.error(
                    f"Final doc ({docs[final_doc_ind].semantic_identifier}) content "
                    "length is less than 0. Removing this doc from the final prompt."
                )
                docs.pop()
            else:
                docs[final_doc_ind].content = tokenizer_trim_content(
                    content=docs[final_doc_ind].content,
                    desired_length=final_doc_content_length,
                    tokenizer=llm_tokenizer,
                )
        else:
            # For regular search, don't truncate the final document unless it's the only one
            # If it's not the only one, we can throw it away, if it's the only one, we have to truncate
            if final_doc_ind != 0:
                docs = docs[:final_doc_ind]
            else:
                docs[0].content = tokenizer_trim_content(
                    content=docs[0].content,
                    desired_length=token_limit - _METADATA_TOKEN_ESTIMATE,
                    tokenizer=llm_tokenizer,
                )
                docs = [docs[0]]

    return docs


def prune_documents(
    docs: list[LlmDoc],
    doc_relevance_list: list[bool] | None,
    prompt_config: PromptConfig,
    llm_config: LLMConfig,
    question: str,
    document_pruning_config: DocumentPruningConfig,
) -> list[LlmDoc]:
    if doc_relevance_list is not None:
        assert len(docs) == len(doc_relevance_list)

    # Drop older-version duplicates of the same docs page before anything else,
    # so the freed context slots get filled by distinct sources during pruning.
    docs, doc_relevance_list = dedupe_doc_versions(docs, doc_relevance_list)

    doc_token_limit = _compute_limit(
        prompt_config=prompt_config,
        llm_config=llm_config,
        question=question,
        max_chunks=document_pruning_config.max_chunks,
        max_window_percentage=document_pruning_config.max_window_percentage,
        max_tokens=document_pruning_config.max_tokens,
        tool_token_count=document_pruning_config.tool_num_tokens,
    )
    return _apply_pruning(
        docs=docs,
        doc_relevance_list=doc_relevance_list,
        token_limit=doc_token_limit,
        is_manually_selected_docs=document_pruning_config.is_manually_selected_docs,
        use_sections=document_pruning_config.use_sections,
        using_tool_message=document_pruning_config.using_tool_message,
    )
