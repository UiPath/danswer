"""Unit tests for get_doc_ids_to_update — the content-hash + timestamp skip
logic that decides which documents actually need (re)indexing.

Covers the new content-hash skip (so timestamp churn like Salesforce's
LastModifiedDate doesn't force a full re-index) AND the backward-compatible
fallback to the original doc_updated_at behavior for rows with no stored hash.
"""
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace

from danswer.configs.constants import DocumentSource
from danswer.connectors.models import Document
from danswer.connectors.models import Section
from danswer.indexing.indexing_pipeline import get_doc_ids_to_update


OLD = datetime(2024, 1, 1, tzinfo=timezone.utc)
NEW = datetime(2024, 6, 1, tzinfo=timezone.utc)


def _doc(doc_id: str, text: str, updated_at: datetime | None = NEW) -> Document:
    return Document(
        id=doc_id,
        sections=[Section(text=text, link=None)],
        source=DocumentSource.SALESFORCE,
        semantic_identifier=doc_id,
        metadata={},
        doc_updated_at=updated_at,
    )


def _db_doc(doc_id: str, content_hash: str | None, updated_at: datetime | None):
    # get_doc_ids_to_update only reads .id, .indexed_content_hash, .doc_updated_at
    return SimpleNamespace(
        id=doc_id, indexed_content_hash=content_hash, doc_updated_at=updated_at
    )


def _ids(docs: list[Document]) -> set[str]:
    return {d.id for d in docs}


def test_new_document_is_updatable() -> None:
    doc = _doc("a", "hello")
    assert _ids(get_doc_ids_to_update([doc], db_docs=[])) == {"a"}


def test_unchanged_content_is_skipped_even_when_timestamp_advances() -> None:
    # The Salesforce case: LastModifiedDate moved forward but content is identical.
    doc = _doc("a", "hello", updated_at=NEW)
    db = _db_doc("a", content_hash=doc.get_content_hash(), updated_at=OLD)
    assert get_doc_ids_to_update([doc], db_docs=[db]) == []


def test_changed_content_is_updatable() -> None:
    doc = _doc("a", "new text", updated_at=NEW)
    stale_hash = _doc("a", "old text").get_content_hash()
    db = _db_doc("a", content_hash=stale_hash, updated_at=OLD)
    assert _ids(get_doc_ids_to_update([doc], db_docs=[db])) == {"a"}


def test_backcompat_null_hash_skips_when_not_newer() -> None:
    # Pre-existing row (no stored hash): original updated_at behavior applies.
    doc = _doc("a", "hello", updated_at=OLD)
    db = _db_doc("a", content_hash=None, updated_at=NEW)
    assert get_doc_ids_to_update([doc], db_docs=[db]) == []


def test_backcompat_null_hash_updates_when_newer() -> None:
    doc = _doc("a", "hello", updated_at=NEW)
    db = _db_doc("a", content_hash=None, updated_at=OLD)
    assert _ids(get_doc_ids_to_update([doc], db_docs=[db])) == {"a"}
