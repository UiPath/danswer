import json
import re
from collections.abc import Callable

from danswer.llm.interfaces import LLM
from danswer.llm.utils import dict_based_prompt_to_langchain_prompt
from danswer.llm.utils import message_to_string
from danswer.prompts.llm_chunk_filter import CHUNK_FILTER_PROMPT
from danswer.prompts.llm_chunk_filter import LISTWISE_CHUNK_FILTER_PROMPT
from danswer.prompts.llm_chunk_filter import NONUSEFUL_PAT
from danswer.utils.logger import setup_logger
from danswer.utils.threadpool_concurrency import run_functions_tuples_in_parallel

logger = setup_logger()


def _parse_useful_indices(model_output: str, count: int) -> set[int] | None:
    """Parse the listwise filter's reply into a set of 1-based useful indices.

    Returns None when no JSON array can be found (a parse failure → caller
    should fail OPEN and keep all chunks). An explicitly empty array `[]` is a
    valid "none useful" answer and returns an empty set (not None).
    """
    match = re.search(r"\[[\s\d,]*\]", model_output)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return {
        int(n) for n in parsed if isinstance(n, (int, float)) and 1 <= int(n) <= count
    }


def llm_eval_chunks_listwise(
    query: str, chunk_contents: list[str], llm: LLM
) -> list[bool]:
    """Judge all chunks in a SINGLE LLM call (vs one call per chunk).

    Returns a parallel list of booleans. Fails OPEN: on any error or an
    unparseable reply, every chunk is kept (True) — same philosophy as the
    per-chunk path ("better to trust the (re)ranking if the LLM fails").
    """
    if not chunk_contents:
        return []

    sections = "\n\n".join(
        f"Section {i + 1}:\n```\n{content}\n```"
        for i, content in enumerate(chunk_contents)
    )
    messages = [
        {
            "role": "user",
            "content": LISTWISE_CHUNK_FILTER_PROMPT.format(
                count=len(chunk_contents), sections=sections, user_query=query
            ),
        }
    ]
    filled_prompt = dict_based_prompt_to_langchain_prompt(messages)
    try:
        model_output = message_to_string(llm.invoke(filled_prompt))
    except Exception:
        logger.exception("Listwise relevance filter call failed — keeping all chunks")
        return [True] * len(chunk_contents)

    useful = _parse_useful_indices(model_output, len(chunk_contents))
    if useful is None:
        logger.warning(
            "Could not parse listwise relevance filter output — keeping all chunks"
        )
        return [True] * len(chunk_contents)

    return [(i + 1) in useful for i in range(len(chunk_contents))]


def llm_eval_chunk(query: str, chunk_content: str, llm: LLM) -> bool:
    def _get_usefulness_messages() -> list[dict[str, str]]:
        messages = [
            {
                "role": "user",
                "content": CHUNK_FILTER_PROMPT.format(
                    chunk_text=chunk_content, user_query=query
                ),
            },
        ]

        return messages

    def _extract_usefulness(model_output: str) -> bool:
        """Default useful if the LLM doesn't match pattern exactly
        This is because it's better to trust the (re)ranking if LLM fails"""
        if model_output.strip().strip('"').lower() == NONUSEFUL_PAT.lower():
            return False
        return True

    messages = _get_usefulness_messages()
    filled_llm_prompt = dict_based_prompt_to_langchain_prompt(messages)
    # When running in a batch, it takes as long as the longest thread
    # And when running a large batch, one may fail and take the whole timeout
    # instead cap it to 5 seconds
    model_output = message_to_string(llm.invoke(filled_llm_prompt))
    logger.debug(model_output)

    return _extract_usefulness(model_output)


def llm_batch_eval_chunks(
    query: str, chunk_contents: list[str], llm: LLM, use_threads: bool = True
) -> list[bool]:
    if use_threads:
        functions_with_args: list[tuple[Callable, tuple]] = [
            (llm_eval_chunk, (query, chunk_content, llm))
            for chunk_content in chunk_contents
        ]

        logger.debug(
            "Running LLM usefulness eval in parallel (following logging may be out of order)"
        )
        parallel_results = run_functions_tuples_in_parallel(
            functions_with_args, allow_failures=True
        )

        # In case of failure/timeout, don't throw out the chunk
        return [True if item is None else item for item in parallel_results]

    else:
        return [
            llm_eval_chunk(query, chunk_content, llm)
            for chunk_content in chunk_contents
        ]
