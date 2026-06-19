"""Tests for versioned-docs dedup in the final LLM source-doc selection.

Documentation sites (docs.uipath.com) publish the same page under one URL per
product version. Retrieval floods the LLM context with near-identical copies,
crowding out distinct sources and making the bot intermittently fail to cite.
dedupe_doc_versions keeps only the newest version's chunk(s) per page, scoped to
docs URLs — every other source must pass through untouched.
"""
from datetime import datetime

from danswer.chat.models import LlmDoc
from danswer.configs.constants import DocumentSource
from danswer.llm.answering import doc_pruning
from danswer.llm.answering.doc_pruning import _docs_page_and_version
from danswer.llm.answering.doc_pruning import _docs_version_sort_key
from danswer.llm.answering.doc_pruning import dedupe_doc_versions

AS = "https://docs.uipath.com/automation-suite/automation-suite"
PAGE = "installation-guide/how-to-delete-images-from-the-old-installer-after-upgrade"


def _doc(url: str, source: DocumentSource = DocumentSource.WEB) -> LlmDoc:
    return LlmDoc(
        document_id=url,
        content="content",
        blurb="blurb",
        semantic_identifier="How to delete images",
        source_type=source,
        metadata={},
        updated_at=None,
        link=url,
        source_links={0: url},
    )


# ---- version ordering ------------------------------------------------------
def test_version_sort_new_scheme_outranks_old() -> None:
    # 2.2510 (new scheme, current) must be newer than 2024.10 (old scheme).
    assert _docs_version_sort_key("2.2510") > _docs_version_sort_key("2024.10")


def test_version_sort_old_scheme_internal_order() -> None:
    keys = [
        _docs_version_sort_key(v)
        for v in ["2022.4", "2022.10", "2023.4", "2023.10", "2024.10"]
    ]
    assert keys == sorted(keys)  # already newest-last -> strictly increasing


def test_version_sort_latest_is_highest() -> None:
    for v in ["2.2510", "2024.10", "2023.10", "9.9999"]:
        assert _docs_version_sort_key("latest") > _docs_version_sort_key(v)


def test_version_sort_unrecognized_is_lowest() -> None:
    assert _docs_version_sort_key("nonsense") < _docs_version_sort_key("2022.4")


# ---- page/version parsing --------------------------------------------------
def test_parse_strips_version_segment() -> None:
    page, ver = _docs_page_and_version(f"{AS}/2024.10/{PAGE}")
    assert ver == "2024.10"
    assert "2024.10" not in page
    # same page, different version -> identical page_key
    page2, _ = _docs_page_and_version(f"{AS}/2.2510/{PAGE}")
    assert page == page2


def test_parse_non_docs_url_returns_none() -> None:
    assert _docs_page_and_version("https://example.com/foo/2024.10/bar") is None


def test_parse_docs_url_without_version_returns_none() -> None:
    assert _docs_page_and_version("https://docs.uipath.com/overview") is None


# ---- dedup behavior --------------------------------------------------------
def test_keeps_only_newest_version_of_a_page() -> None:
    docs = [
        _doc(f"{AS}/2023.10/{PAGE}"),
        _doc(f"{AS}/2024.10/{PAGE}"),
        _doc(f"{AS}/2.2510/{PAGE}"),  # newest
        _doc(f"{AS}/2022.4/{PAGE}"),
    ]
    kept, _ = dedupe_doc_versions(docs, None)
    assert [d.document_id for d in kept] == [f"{AS}/2.2510/{PAGE}"]


def test_distinct_pages_all_survive() -> None:
    a = f"{AS}/2024.10/guide/page-a"
    b = f"{AS}/2024.10/guide/page-b"
    kept, _ = dedupe_doc_versions([_doc(a), _doc(b)], None)
    assert {d.document_id for d in kept} == {a, b}


def test_keeps_multiple_chunks_of_newest_version() -> None:
    # Two distinct chunks of the same newest-version page both survive.
    d1 = _doc(f"{AS}/2.2510/{PAGE}")
    d2 = _doc(f"{AS}/2.2510/{PAGE}")
    d2.content = "different chunk"
    old = _doc(f"{AS}/2024.10/{PAGE}")
    kept, _ = dedupe_doc_versions([d1, old, d2], None)
    assert len(kept) == 2
    assert all(d.document_id == f"{AS}/2.2510/{PAGE}" for d in kept)


def test_non_docs_sources_untouched() -> None:
    slack = _doc("slack-msg-123", source=DocumentSource.SLACK)
    web = _doc("https://other.com/x/2024.10/y", source=DocumentSource.WEB)
    docs = [_doc(f"{AS}/2024.10/{PAGE}"), _doc(f"{AS}/2.2510/{PAGE}"), slack, web]
    kept, _ = dedupe_doc_versions(docs, None)
    ids = [d.document_id for d in kept]
    assert "slack-msg-123" in ids
    assert "https://other.com/x/2024.10/y" in ids
    assert f"{AS}/2024.10/{PAGE}" not in ids  # older docs version dropped


def test_relevance_list_filtered_in_lockstep() -> None:
    docs = [
        _doc(f"{AS}/2024.10/{PAGE}"),  # dropped
        _doc(f"{AS}/2.2510/{PAGE}"),  # kept (relevant)
        _doc("slack-1", source=DocumentSource.SLACK),  # kept
    ]
    rel = [True, True, False]
    kept, kept_rel = dedupe_doc_versions(docs, rel)
    assert len(kept) == len(kept_rel) == 2
    assert kept_rel == [True, False]


def test_disabled_when_substr_empty(monkeypatch) -> None:
    monkeypatch.setattr(doc_pruning, "DOCS_VERSION_DEDUP_URL_SUBSTR", "")
    docs = [_doc(f"{AS}/2023.10/{PAGE}"), _doc(f"{AS}/2.2510/{PAGE}")]
    kept, _ = dedupe_doc_versions(docs, None)
    assert len(kept) == 2  # no dedup when disabled
