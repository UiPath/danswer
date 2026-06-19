"""Regression tests for translate_citations.

Guards the selected-docs `KeyError` fix: the LLM can emit a citation for a
document that isn't in this turn's reference docs (e.g. when chatting with a
subset of selected documents, it cites a doc from earlier in the conversation).
That must be skipped gracefully, not crash the whole response (which previously
surfaced as the misleading "Failed to parse LLM output").
"""
from types import SimpleNamespace

from danswer.chat.models import CitationInfo
from danswer.chat.process_message import translate_citations


def _db_doc(db_id: int, document_id: str) -> SimpleNamespace:
    # translate_citations only reads .id and .document_id off each db_doc.
    return SimpleNamespace(id=db_id, document_id=document_id)


def test_maps_citation_num_to_saved_doc_id() -> None:
    db_docs = [_db_doc(101, "DOC_A"), _db_doc(102, "DOC_B")]
    citations = [
        CitationInfo(citation_num=1, document_id="DOC_A"),
        CitationInfo(citation_num=2, document_id="DOC_B"),
    ]
    assert translate_citations(citations, db_docs) == {1: 101, 2: 102}


def test_skips_citation_for_unknown_document_id() -> None:
    # The fix: an unknown document_id is skipped, not a KeyError.
    db_docs = [_db_doc(101, "DOC_A")]
    citations = [
        CitationInfo(citation_num=1, document_id="DOC_A"),
        CitationInfo(citation_num=2, document_id="DOC_NOT_IN_THIS_TURN"),
    ]
    assert translate_citations(citations, db_docs) == {1: 101}


def test_all_unknown_citations_yields_empty_map_without_error() -> None:
    db_docs = [_db_doc(101, "DOC_A")]
    citations = [CitationInfo(citation_num=1, document_id="GHOST")]
    assert translate_citations(citations, db_docs) == {}


def test_first_db_doc_for_a_document_id_wins() -> None:
    # Duplicate document_id across db_docs -> the first (UI order) is cited.
    db_docs = [_db_doc(101, "DOC_A"), _db_doc(202, "DOC_A")]
    citations = [CitationInfo(citation_num=1, document_id="DOC_A")]
    assert translate_citations(citations, db_docs) == {1: 101}
