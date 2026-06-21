from collections import defaultdict
from collections.abc import Callable
from collections.abc import Generator
from typing import cast

from pydantic import BaseModel
from sqlalchemy.orm import Session

from danswer.configs.chat_configs import MULTILINGUAL_QUERY_EXPANSION
from danswer.configs.chat_configs import PROTECTED_SOURCES
from danswer.configs.chat_configs import SOURCE_RESERVED_RETRIEVAL_SLOTS
from danswer.configs.constants import DocumentSource
from danswer.db.embedding_model import get_current_db_embedding_model
from danswer.db.models import User
from danswer.document_index.factory import get_default_document_index
from danswer.llm.interfaces import LLM
from danswer.search.enums import QueryFlow
from danswer.search.enums import SearchType
from danswer.search.models import IndexFilters
from danswer.search.models import InferenceChunk
from danswer.search.models import InferenceSection
from danswer.search.models import RerankMetricsContainer
from danswer.search.models import RetrievalMetricsContainer
from danswer.search.models import SearchQuery
from danswer.search.models import SearchRequest
from danswer.search.postprocessing.postprocessing import search_postprocessing
from danswer.search.preprocessing.preprocessing import retrieval_preprocessing
from danswer.search.retrieval.search_runner import retrieve_chunks
from danswer.utils.logger import setup_logger
from danswer.utils.threadpool_concurrency import run_functions_tuples_in_parallel


logger = setup_logger()


def protected_source_topup(
    existing: list[InferenceChunk],
    candidates: list[InferenceChunk],
    reserved: int,
    protected_sources: set[DocumentSource],
) -> list[InferenceChunk]:
    """Pick up to `reserved` protected-source chunks from `candidates` that are not
    already present in `existing`, to top the candidate set up to the reservation.

    Pure (no I/O) so it's unit-testable. Returns only the chunks to ADD (callers
    append them). De-dupes by `unique_id` against `existing` and within the result.
    """
    if reserved <= 0 or not protected_sources:
        return []
    present = sum(1 for c in existing if c.source_type in protected_sources)
    need = reserved - present
    if need <= 0:
        return []

    seen = {c.unique_id for c in existing}
    added: list[InferenceChunk] = []
    for c in candidates:
        if len(added) >= need:
            break
        if c.source_type not in protected_sources:
            continue
        if c.unique_id in seen:
            continue
        seen.add(c.unique_id)
        added.append(c)
    return added


class ChunkRange(BaseModel):
    chunk: InferenceChunk
    start: int
    end: int
    combined_content: str | None = None


def merge_chunk_intervals(chunk_ranges: list[ChunkRange]) -> list[ChunkRange]:
    """This acts on a single document to merge the overlapping ranges of sections
    Algo explained here for easy understanding: https://leetcode.com/problems/merge-intervals
    """
    sorted_ranges = sorted(chunk_ranges, key=lambda x: x.start)

    ans: list[ChunkRange] = []

    for chunk_range in sorted_ranges:
        if not ans or ans[-1].end < chunk_range.start:
            ans.append(chunk_range)
        else:
            ans[-1].end = max(ans[-1].end, chunk_range.end)

    return ans


class SearchPipeline:
    def __init__(
        self,
        search_request: SearchRequest,
        user: User | None,
        llm: LLM,
        fast_llm: LLM,
        db_session: Session,
        bypass_acl: bool = False,  # NOTE: VERY DANGEROUS, USE WITH CAUTION
        retrieval_metrics_callback: Callable[[RetrievalMetricsContainer], None]
        | None = None,
        rerank_metrics_callback: Callable[[RerankMetricsContainer], None] | None = None,
    ):
        self.search_request = search_request
        self.user = user
        self.llm = llm
        self.fast_llm = fast_llm
        self.db_session = db_session
        self.bypass_acl = bypass_acl
        self.retrieval_metrics_callback = retrieval_metrics_callback
        self.rerank_metrics_callback = rerank_metrics_callback

        self.embedding_model = get_current_db_embedding_model(db_session)
        self.document_index = get_default_document_index(
            primary_index_name=self.embedding_model.index_name,
            secondary_index_name=None,
        )

        self._search_query: SearchQuery | None = None
        self._predicted_search_type: SearchType | None = None
        self._predicted_flow: QueryFlow | None = None

        self._retrieved_chunks: list[InferenceChunk] | None = None
        self._retrieved_sections: list[InferenceSection] | None = None
        self._reranked_chunks: list[InferenceChunk] | None = None
        self._reranked_sections: list[InferenceSection] | None = None
        self._relevant_chunk_indices: list[int] | None = None

        # If chunks have been merged, the LLM filter flow no longer applies
        # as the indices no longer match. Can be implemented later as needed
        self.ran_merge_chunk = False

        # generator state
        self._postprocessing_generator: Generator[
            list[InferenceChunk] | list[str], None, None
        ] | None = None

    def _combine_chunks(self, post_rerank: bool) -> list[InferenceSection]:
        if not post_rerank and self._retrieved_sections:
            return self._retrieved_sections
        if post_rerank and self._reranked_sections:
            return self._reranked_sections

        if not post_rerank:
            chunks = self.retrieved_chunks
        else:
            chunks = self.reranked_chunks

        if self._search_query is None:
            # Should never happen
            raise RuntimeError("Failed in Query Preprocessing")

        functions_with_args: list[tuple[Callable, tuple]] = []
        final_inference_sections = []

        # Nothing to combine, just return the chunks
        if (
            not self._search_query.chunks_above
            and not self._search_query.chunks_below
            and not self._search_query.full_doc
        ):
            return [InferenceSection.from_chunk(chunk) for chunk in chunks]

        # If chunk merges have been run, LLM reranking loses meaning
        # Needs reimplementation, out of scope for now
        self.ran_merge_chunk = True

        # Full doc setting takes priority
        if self._search_query.full_doc:
            seen_document_ids = set()
            unique_chunks = []
            for chunk in chunks:
                if chunk.document_id not in seen_document_ids:
                    seen_document_ids.add(chunk.document_id)
                    unique_chunks.append(chunk)

                    functions_with_args.append(
                        (
                            self.document_index.id_based_retrieval,
                            (
                                chunk.document_id,
                                None,  # Start chunk ind
                                None,  # End chunk ind
                                # There is no chunk level permissioning, this expansion around chunks
                                # can be assumed to be safe
                                IndexFilters(access_control_list=None),
                            ),
                        )
                    )

            list_inference_chunks = run_functions_tuples_in_parallel(
                functions_with_args, allow_failures=False
            )

            for ind, chunk in enumerate(unique_chunks):
                inf_chunks = list_inference_chunks[ind]
                combined_content = "\n".join([chunk.content for chunk in inf_chunks])
                final_inference_sections.append(
                    InferenceSection.from_chunk(chunk, content=combined_content)
                )

            return final_inference_sections

        # General flow:
        # - Combine chunks into lists by document_id
        # - For each document, run merge-intervals to get combined ranges
        # - Fetch all of the new chunks with contents for the combined ranges
        # - Map it back to the combined ranges (which each know their "center" chunk)
        # - Reiterate the chunks again and map to the results above based on the chunk.
        #   This maintains the original chunks ordering. Note, we cannot simply sort by score here
        #   as reranking flow may wipe the scores for a lot of the chunks.
        doc_chunk_ranges_map = defaultdict(list)
        for chunk in chunks:
            doc_chunk_ranges_map[chunk.document_id].append(
                ChunkRange(
                    chunk=chunk,
                    start=max(0, chunk.chunk_id - self._search_query.chunks_above),
                    # No max known ahead of time, filter will handle this anyway
                    end=chunk.chunk_id + self._search_query.chunks_below,
                )
            )

        merged_ranges = [
            merge_chunk_intervals(ranges) for ranges in doc_chunk_ranges_map.values()
        ]
        reverse_map = {r.chunk: r for doc_ranges in merged_ranges for r in doc_ranges}

        for chunk_range in reverse_map.values():
            functions_with_args.append(
                (
                    self.document_index.id_based_retrieval,
                    (
                        chunk_range.chunk.document_id,
                        chunk_range.start,
                        chunk_range.end,
                        # There is no chunk level permissioning, this expansion around chunks
                        # can be assumed to be safe
                        IndexFilters(access_control_list=None),
                    ),
                )
            )

        # list of list of inference chunks where the inner list needs to be combined for content
        list_inference_chunks = run_functions_tuples_in_parallel(
            functions_with_args, allow_failures=False
        )

        for ind, chunk_range in enumerate(reverse_map.values()):
            inf_chunks = list_inference_chunks[ind]
            combined_content = "\n".join([chunk.content for chunk in inf_chunks])
            chunk_range.combined_content = combined_content

        for chunk in chunks:
            if chunk not in reverse_map:
                continue
            chunk_range = reverse_map[chunk]
            final_inference_sections.append(
                InferenceSection.from_chunk(
                    chunk_range.chunk, content=chunk_range.combined_content
                )
            )

        return final_inference_sections

    """Pre-processing"""

    def _run_preprocessing(self) -> None:
        (
            final_search_query,
            predicted_search_type,
            predicted_flow,
        ) = retrieval_preprocessing(
            search_request=self.search_request,
            user=self.user,
            llm=self.llm,
            db_session=self.db_session,
            bypass_acl=self.bypass_acl,
        )
        self._predicted_search_type = predicted_search_type
        self._predicted_flow = predicted_flow
        self._search_query = final_search_query

    @property
    def search_query(self) -> SearchQuery:
        if self._search_query is not None:
            return self._search_query

        self._run_preprocessing()
        return cast(SearchQuery, self._search_query)

    @property
    def predicted_search_type(self) -> SearchType:
        if self._predicted_search_type is not None:
            return self._predicted_search_type

        self._run_preprocessing()
        return cast(SearchType, self._predicted_search_type)

    @property
    def predicted_flow(self) -> QueryFlow:
        if self._predicted_flow is not None:
            return self._predicted_flow

        self._run_preprocessing()
        return cast(QueryFlow, self._predicted_flow)

    """Retrieval"""

    @property
    def retrieved_chunks(self) -> list[InferenceChunk]:
        if self._retrieved_chunks is not None:
            return self._retrieved_chunks

        chunks = retrieve_chunks(
            query=self.search_query,
            document_index=self.document_index,
            db_session=self.db_session,
            hybrid_alpha=self.search_request.hybrid_alpha,
            multilingual_expansion_str=MULTILINGUAL_QUERY_EXPANSION,
            retrieval_metrics_callback=self.retrieval_metrics_callback,
        )

        # Recall guarantee for curated sources (see SOURCE_RESERVED_RETRIEVAL_SLOTS).
        chunks = self._supplement_protected_sources(chunks)

        self._retrieved_chunks = chunks
        return cast(list[InferenceChunk], self._retrieved_chunks)

    def _supplement_protected_sources(
        self, chunks: list[InferenceChunk]
    ) -> list[InferenceChunk]:
        """Ensure up to SOURCE_RESERVED_RETRIEVAL_SLOTS chunks from PROTECTED_SOURCES
        are in the candidate set. A chatty source (e.g. Slack) can otherwise fill
        every top hit, starving curated sources (web/KB/OutSystems) that retrieval
        would surface only on a source-scoped query — so we run ONE extra retrieval
        restricted to the protected sources and merge the top results in.

        Scope-safe: the supplemental query reuses the SAME filters as the main query
        (ACL + persona document-set fence) and only ADDS a source_type restriction
        (intersected with any existing source filter), so nothing outside the
        query's existing scope is ever introduced. Universal — runs for every
        persona in both the chat and Slack flows, since both funnel through here.
        """
        reserved = SOURCE_RESERVED_RETRIEVAL_SLOTS
        if reserved <= 0 or not PROTECTED_SOURCES:
            return chunks

        # Map configured source strings -> DocumentSource (skip any unknowns).
        valid = DocumentSource._value2member_map_
        protected_ds = [valid[s] for s in PROTECTED_SOURCES if s in valid]
        if not protected_ds:
            return chunks
        protected_set = set(protected_ds)

        # Cheap exit if the reservation is already satisfied.
        if sum(1 for c in chunks if c.source_type in protected_set) >= reserved:
            return chunks

        # Restrict to protected sources, intersected with any existing source filter
        # so we never widen scope. ACL + document_set fence are preserved as-is.
        existing_src = self.search_query.filters.source_type
        allowed = (
            [s for s in protected_ds if s in existing_src]
            if existing_src
            else protected_ds
        )
        if not allowed:
            return chunks
        # SearchQuery / IndexFilters are immutable (frozen pydantic) — rebuild via
        # copy(update=...) rather than mutating in place.
        supp_filters = self.search_query.filters.copy(update={"source_type": allowed})
        supp_query = self.search_query.copy(
            update={"filters": supp_filters, "num_hits": max(reserved * 4, 10)}
        )

        supp_chunks = retrieve_chunks(
            query=supp_query,
            document_index=self.document_index,
            db_session=self.db_session,
            hybrid_alpha=self.search_request.hybrid_alpha,
            multilingual_expansion_str=MULTILINGUAL_QUERY_EXPANSION,
            retrieval_metrics_callback=self.retrieval_metrics_callback,
        )

        added = protected_source_topup(chunks, supp_chunks, reserved, protected_set)
        if added:
            logger.info(
                "Source-reserved retrieval: injected %d protected-source chunk(s) "
                "from %s (reservation=%d)",
                len(added),
                [c.source_type.value for c in added],
                reserved,
            )
        return chunks + added

    @property
    def retrieved_sections(self) -> list[InferenceSection]:
        # Calls retrieved_chunks inside
        self._retrieved_sections = self._combine_chunks(post_rerank=False)
        return self._retrieved_sections

    """Post-Processing"""

    @property
    def reranked_chunks(self) -> list[InferenceChunk]:
        if self._reranked_chunks is not None:
            return self._reranked_chunks

        self._postprocessing_generator = search_postprocessing(
            search_query=self.search_query,
            retrieved_chunks=self.retrieved_chunks,
            # Use the MAIN llm (not fast_llm) for the relevance filter: it now
            # judges all chunks in one listwise call, and the main model is more
            # reliable at that structured multi-item judgment.
            llm=self.llm,
            rerank_metrics_callback=self.rerank_metrics_callback,
        )
        self._reranked_chunks = cast(
            list[InferenceChunk], next(self._postprocessing_generator)
        )
        return self._reranked_chunks

    @property
    def reranked_sections(self) -> list[InferenceSection]:
        # Calls reranked_chunks inside
        self._reranked_sections = self._combine_chunks(post_rerank=True)
        return self._reranked_sections

    @property
    def relevant_chunk_indices(self) -> list[int]:
        # If chunks have been merged, then we cannot simply rely on the leading chunk
        # relevance, there is no way to get the full relevance of the Section now
        # without running a more token heavy pass. This can be an option but not
        # implementing now.
        if self.ran_merge_chunk:
            return []

        if self._relevant_chunk_indices is not None:
            return self._relevant_chunk_indices

        # run first step of postprocessing generator if not already done
        reranked_docs = self.reranked_chunks

        relevant_chunk_ids = next(
            cast(Generator[list[str], None, None], self._postprocessing_generator)
        )
        self._relevant_chunk_indices = [
            ind
            for ind, chunk in enumerate(reranked_docs)
            if chunk.unique_id in relevant_chunk_ids
        ]
        return self._relevant_chunk_indices

    @property
    def chunk_relevance_list(self) -> list[bool]:
        return [
            True if ind in self.relevant_chunk_indices else False
            for ind in range(len(self.reranked_chunks))
        ]

    @property
    def section_relevance_list(self) -> list[bool]:
        if self.ran_merge_chunk:
            return [False] * len(self.reranked_sections)

        return [
            True if ind in self.relevant_chunk_indices else False
            for ind in range(len(self.reranked_chunks))
        ]
