"""Unit tests for query-time docs version rewrite — rewrite a retrieved versioned
docs link to the newest version of the same page that exists in the index."""
from types import SimpleNamespace

import pytest

from danswer.llm.answering import doc_pruning as dp

BASE = "https://docs.uipath.com/orchestrator/standalone"
PAGE = "installation-guide/maintenance-considerations"


def ldoc(link):
    return SimpleNamespace(link=link)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    """Ignores the WHERE (the Python-side page match is what we're testing) and
    returns every indexed id as a 1-tuple row, like SQLAlchemy .execute().all()."""

    def __init__(self, ids):
        self._ids = ids
        self.calls = 0

    def execute(self, _stmt):
        self.calls += 1
        return _FakeResult([(i,) for i in self._ids])


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(dp, "DOCS_VERSION_DEDUP_URL_SUBSTR", "docs.uipath.com")


def test_versioned_url_parts():
    assert dp._versioned_url_parts(f"{BASE}/2023.10/{PAGE}") == (BASE, "2023.10", PAGE)
    assert dp._versioned_url_parts("https://docs.uipath.com/no/version/here") is None
    assert dp._versioned_url_parts("https://example.com/2024.10/x") is None  # not docs


def test_rewrites_to_latest_indexed_version():
    index = [f"{BASE}/2023.4/{PAGE}", f"{BASE}/2023.10/{PAGE}", f"{BASE}/2025.10/{PAGE}"]
    docs = [ldoc(f"{BASE}/2023.4/{PAGE}")]
    dp.rewrite_docs_links_to_latest(docs, _FakeDB(index))
    assert docs[0].link == f"{BASE}/2025.10/{PAGE}"


def test_noop_when_already_latest():
    index = [f"{BASE}/2023.10/{PAGE}", f"{BASE}/2025.10/{PAGE}"]
    docs = [ldoc(f"{BASE}/2025.10/{PAGE}")]
    dp.rewrite_docs_links_to_latest(docs, _FakeDB(index))
    assert docs[0].link == f"{BASE}/2025.10/{PAGE}"


def test_does_not_cross_slug_variants():
    # "is-maintenance-considerations" is a different page than "maintenance-considerations"
    other = "installation-guide/is-maintenance-considerations"
    index = [f"{BASE}/2025.10/{other}", f"{BASE}/2023.4/{PAGE}"]
    docs = [ldoc(f"{BASE}/2023.4/{PAGE}")]
    dp.rewrite_docs_links_to_latest(docs, _FakeDB(index))
    # no newer version of THIS slug -> unchanged
    assert docs[0].link == f"{BASE}/2023.4/{PAGE}"


def test_non_docs_link_untouched():
    docs = [ldoc("https://Product.slack.com/archives/C1/p123")]
    db = _FakeDB([])
    dp.rewrite_docs_links_to_latest(docs, db)
    assert docs[0].link == "https://Product.slack.com/archives/C1/p123"
    assert db.calls == 0  # no docs pages -> no query


def test_new_scheme_outranks_old():
    index = [f"{BASE}/2025.10/{PAGE}", f"{BASE}/2.2510/{PAGE}"]  # 2.2510 = new scheme
    docs = [ldoc(f"{BASE}/2025.10/{PAGE}")]
    dp.rewrite_docs_links_to_latest(docs, _FakeDB(index))
    assert docs[0].link == f"{BASE}/2.2510/{PAGE}"
