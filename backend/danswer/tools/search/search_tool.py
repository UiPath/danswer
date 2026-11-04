import json
from collections.abc import Generator
from typing import Any
from typing import cast

from pydantic import BaseModel
from sqlalchemy.orm import Session

from danswer.chat.chat_utils import llm_doc_from_inference_section
from danswer.chat.models import DanswerContext
from danswer.chat.models import DanswerContexts
from danswer.chat.models import LlmDoc
from danswer.db.models import Persona
from danswer.db.models import User
from danswer.dynamic_configs.interface import JSON_ro
from danswer.llm.answering.doc_pruning import prune_documents
from danswer.llm.answering.doc_pruning import rewrite_docs_links
from danswer.llm.answering.models import DocumentPruningConfig
from danswer.llm.answering.models import PreviousMessage
from danswer.llm.answering.models import PromptConfig
from danswer.llm.interfaces import LLM
from danswer.search.enums import QueryFlow
from danswer.search.enums import SearchType
from danswer.search.models import IndexFilters
from danswer.search.models import InferenceSection
from danswer.search.models import RetrievalDetails
from danswer.search.models import SearchRequest
from danswer.search.pipeline import SearchPipeline
from danswer.search.versioning import question_version_candidates
from danswer.search.versioning import version_merged_sections
from danswer.search.versioning import version_query_variants
from danswer.secondary_llm_flows.choose_search import check_if_need_search
from danswer.secondary_llm_flows.query_expansion import history_based_query_rephrase
from danswer.tools.search.search_utils import llm_doc_to_dict
from danswer.tools.tool import Tool
from danswer.tools.tool import ToolResponse

SEARCH_RESPONSE_SUMMARY_ID = "search_response_summary"
SEARCH_DOC_CONTENT_ID = "search_doc_content"
SECTION_RELEVANCE_LIST_ID = "section_relevance_list"
FINAL_CONTEXT_DOCUMENTS = "final_context_documents"


class SearchResponseSummary(BaseModel):
    top_sections: list[InferenceSection]
    rephrased_query: str | None = None
    predicted_flow: QueryFlow | None
    predicted_search: SearchType | None
    final_filters: IndexFilters
    recency_bias_multiplier: float


search_tool_description = """
Runs a semantic search over the user's knowledge base. The default behavior is to use this tool. \
The only scenario where you should not use this tool is if:

- There is sufficient information in chat history to FULLY and ACCURATELY answer the query AND \
additional information or details would provide little or no value.
- The query is some form of request that does not require additional information to handle.

HINT: if you are unfamiliar with the user input OR think the user input is a typo, use this tool.
"""


class SearchTool(Tool):
    NAME = "run_search"

    def __init__(
        self,
        db_session: Session,
        user: User | None,
        persona: Persona,
        retrieval_options: RetrievalDetails | None,
        prompt_config: PromptConfig,
        llm: LLM,
        fast_llm: LLM,
        pruning_config: DocumentPruningConfig,
        # if specified, will not actually run a search and will instead return these
        # sections. Used when the user selects specific docs to talk to
        selected_docs: list[LlmDoc] | None = None,
        chunks_above: int = 0,
        chunks_below: int = 0,
        full_doc: bool = False,
        bypass_acl: bool = False,
        # Per-request reranking / relevance-filter overrides. None => let
        # retrieval_preprocessing decide from the global flags + the assistant's
        # settings (Slack / default path). The chat flow passes explicit values
        # derived from the per-conversation toggles + global flags.
        skip_rerank: bool | None = None,
        skip_llm_chunk_filter: bool | None = None,
    ) -> None:
        self.user = user
        self.persona = persona
        self.retrieval_options = retrieval_options
        self.prompt_config = prompt_config
        self.llm = llm
        self.fast_llm = fast_llm
        self.pruning_config = pruning_config

        self.selected_docs = selected_docs

        self.chunks_above = chunks_above
        self.chunks_below = chunks_below
        self.full_doc = full_doc
        self.bypass_acl = bypass_acl
        self.skip_rerank = skip_rerank
        self.skip_llm_chunk_filter = skip_llm_chunk_filter
        self.db_session = db_session

    def name(self) -> str:
        return self.NAME

    """For explicit tool calling"""

    def tool_definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": search_tool_description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to search for",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def build_tool_message_content(
        self, *args: ToolResponse
    ) -> str | list[str | dict[str, Any]]:
        final_context_docs_response = next(
            response for response in args if response.id == FINAL_CONTEXT_DOCUMENTS
        )
        final_context_docs = cast(list[LlmDoc], final_context_docs_response.response)

        return json.dumps(
            {
                "search_results": [
                    llm_doc_to_dict(doc, ind)
                    for ind, doc in enumerate(final_context_docs)
                ]
            }
        )

    """For LLMs that don't support tool calling"""

    def get_args_for_non_tool_calling_llm(
        self,
        query: str,
        history: list[PreviousMessage],
        llm: LLM,
        force_run: bool = False,
    ) -> dict[str, Any] | None:
        if not force_run and not check_if_need_search(
            query=query, history=history, llm=llm
        ):
            return None

        # skip_first_rephrase: when there's no conversation history (a first /
        # single-turn question), search the user's full question verbatim instead
        # of letting the rephraser compress it to bare keywords. The
        # HISTORY_QUERY_REPHRASE prompt deliberately strips queries to "mainly
        # keywords"; for entity lookups that surface many near-duplicate records
        # (e.g. "who is the TAM for pepsi" -> "TAM Pepsi", where only one of ~50
        # "Pepsi" account records holds the field) the compressed query reranks
        # the wrong record into the context window and the answer comes back
        # "not available". The full natural-language question retrieves the right
        # record. Follow-ups (history present) are still rephrased, since they
        # need prior-turn context folded in. This matches the Slack / one-shot
        # flow, which already leaves the first query untouched.
        rephrased_query = history_based_query_rephrase(
            query=query, history=history, llm=llm, skip_first_rephrase=True
        )
        return {"query": rephrased_query}

    """Actual tool execution"""

    def _build_response_for_specified_sections(
        self, query: str
    ) -> Generator[ToolResponse, None, None]:
        if self.selected_docs is None:
            raise ValueError("sections must be specified")

        yield ToolResponse(
            id=SEARCH_RESPONSE_SUMMARY_ID,
            response=SearchResponseSummary(
                rephrased_query=None,
                top_sections=[],
                predicted_flow=None,
                predicted_search=None,
                final_filters=IndexFilters(access_control_list=None),  # dummy filters
                recency_bias_multiplier=1.0,
            ),
        )
        yield ToolResponse(
            id=SECTION_RELEVANCE_LIST_ID,
            response=[i for i in range(len(self.selected_docs))],
        )
        yield ToolResponse(
            id=FINAL_CONTEXT_DOCUMENTS,
            response=prune_documents(
                docs=self.selected_docs,
                doc_relevance_list=None,
                prompt_config=self.prompt_config,
                llm_config=self.llm.config,
                question=query,
                document_pruning_config=self.pruning_config,
            ),
        )

    def _run_pipeline(self, query: str) -> SearchPipeline:
        return SearchPipeline(
            search_request=SearchRequest(
                query=query,
                human_selected_filters=(
                    self.retrieval_options.filters if self.retrieval_options else None
                ),
                persona=self.persona,
                offset=self.retrieval_options.offset
                if self.retrieval_options
                else None,
                limit=self.retrieval_options.limit if self.retrieval_options else None,
                chunks_above=self.chunks_above,
                chunks_below=self.chunks_below,
                full_doc=self.full_doc,
                skip_rerank=self.skip_rerank,
                skip_llm_chunk_filter=self.skip_llm_chunk_filter,
            ),
            user=self.user,
            llm=self.llm,
            fast_llm=self.fast_llm,
            bypass_acl=self.bypass_acl,
            db_session=self.db_session,
        )

    def _version_merged_retrieval(
        self, query: str
    ) -> tuple[list[InferenceSection], list[int], SearchPipeline]:
        """Version-normalized retrieval via the shared `version_merged_sections` helper
        (same one deep-RAG uses). Retrieves each corpus-form variant, keeps only on-version
        hits, merges (deduped, score-sorted). LLM-relevance carries over: a merged section
        is relevant if it was relevant in the variant pipeline it came from. Returns the
        merged sections, their relevant indices, and the first variant's pipeline (for query
        metadata — predicted flow, filters, recency)."""
        relevant_keys: set[tuple[str, int]] = set()
        meta: list[SearchPipeline | None] = [None]

        def retrieve_one(variant: str) -> list[InferenceSection]:
            pipeline = self._run_pipeline(variant)
            if meta[0] is None:
                meta[0] = pipeline
            relevant = set(pipeline.relevant_chunk_indices or [])
            sections = pipeline.reranked_sections
            for i, section in enumerate(sections):
                if i in relevant:
                    relevant_keys.add((section.document_id, section.chunk_id))
            return sections

        merged = version_merged_sections(retrieve_one, query)
        relevant_indices = [
            idx
            for idx, s in enumerate(merged)
            if (s.document_id, s.chunk_id) in relevant_keys
        ]
        assert meta[0] is not None  # version_query_variants never returns an empty list
        return merged, relevant_indices, meta[0]

    def run(self, **kwargs: str) -> Generator[ToolResponse, None, None]:
        query = cast(str, kwargs["query"])

        if self.selected_docs:
            yield from self._build_response_for_specified_sections(query)
            return

        # Version-normalized retrieval: a version token in the query is rewritten to its
        # corpus form(s) — Automation Suite "2.NNMM" and/or standalone "YYYY.MM" — and each
        # variant is retrieved, on-version-filtered and merged, so the query lands on the
        # right release's docs instead of whatever version the raw token happens to rank
        # (a literal "2025.10" matches no AS doc, which are labeled 2.2510). No version
        # token -> a single pipeline on the raw query (behavior unchanged).
        variants = version_query_variants(query)
        if len(variants) == 1 and variants[0] == query:
            pipeline = self._run_pipeline(query)
            sections = pipeline.reranked_sections
            relevant_indices = pipeline.relevant_chunk_indices
            meta = pipeline
        else:
            sections, relevant_indices, meta = self._version_merged_retrieval(query)
            if not sections:
                # nothing survived the on-version filter (no doc for that version) ->
                # fall back to the raw query so we never return empty where a plain
                # search would have found something.
                pipeline = self._run_pipeline(query)
                sections = pipeline.reranked_sections
                relevant_indices = pipeline.relevant_chunk_indices
                meta = pipeline

        yield ToolResponse(
            id=SEARCH_RESPONSE_SUMMARY_ID,
            response=SearchResponseSummary(
                rephrased_query=query,
                top_sections=sections,
                predicted_flow=meta.predicted_flow,
                predicted_search=meta.predicted_search_type,
                final_filters=meta.search_query.filters,
                recency_bias_multiplier=meta.search_query.recency_bias_multiplier,
            ),
        )
        yield ToolResponse(
            id=SEARCH_DOC_CONTENT_ID,
            response=DanswerContexts(
                contexts=[
                    DanswerContext(
                        content=section.content,
                        document_id=section.document_id,
                        semantic_identifier=section.semantic_identifier,
                        blurb=section.blurb,
                    )
                    for section in sections
                ]
            ),
        )
        yield ToolResponse(
            id=SECTION_RELEVANCE_LIST_ID,
            response=relevant_indices,
        )

        llm_docs = [llm_doc_from_inference_section(section) for section in sections]
        final_context_documents = prune_documents(
            docs=llm_docs,
            doc_relevance_list=[
                ind in relevant_indices for ind in range(len(llm_docs))
            ],
            prompt_config=self.prompt_config,
            llm_config=self.llm.config,
            question=query,
            document_pruning_config=self.pruning_config,
        )
        # Rewrite versioned-docs links to the right version of the same page: the
        # version the question asked about if it named one (scheme-aware candidates),
        # else the newest indexed (retrieval can surface a stale version when several
        # exist).
        rewrite_docs_links(
            final_context_documents,
            self.db_session,
            target_versions=question_version_candidates(query),
        )
        yield ToolResponse(id=FINAL_CONTEXT_DOCUMENTS, response=final_context_documents)

    def final_result(self, *args: ToolResponse) -> JSON_ro:
        final_docs = cast(
            list[LlmDoc],
            next(arg.response for arg in args if arg.id == FINAL_CONTEXT_DOCUMENTS),
        )
        # NOTE: need to do this json.loads(doc.json()) stuff because there are some
        # subfields that are not serializable by default (datetime)
        # this forces pydantic to make them JSON serializable for us
        return [json.loads(doc.json()) for doc in final_docs]
