"""Unit tests for the source-reserved retrieval top-up.

`protected_source_topup` is the pure core of the recall guarantee: given the main
candidate set and a source-scoped supplemental retrieval, it decides which (if any)
protected-source chunks to inject so the candidate set holds up to `reserved` of
them. Tested with stub chunks (only `.source_type` and `.unique_id` are read).
"""
from types import SimpleNamespace

from danswer.configs.constants import DocumentSource
from danswer.search.pipeline import protected_source_topup

OS = DocumentSource.OUTSYSTEMS
SLACK = DocumentSource.SLACK
WEB = DocumentSource.WEB
PROTECTED = {OS, WEB}


def chunk(uid: str, source: DocumentSource) -> SimpleNamespace:
    return SimpleNamespace(unique_id=uid, source_type=source)


def uids(chunks: list) -> list[str]:
    return [c.unique_id for c in chunks]


def test_injects_up_to_reserved_when_none_present() -> None:
    existing = [chunk(f"s{i}", SLACK) for i in range(50)]
    candidates = [chunk(f"o{i}", OS) for i in range(5)]
    added = protected_source_topup(existing, candidates, reserved=3, protected_sources=PROTECTED)
    assert uids(added) == ["o0", "o1", "o2"]


def test_tops_up_only_the_shortfall_when_some_present() -> None:
    existing = [chunk("o_present", OS)] + [chunk(f"s{i}", SLACK) for i in range(10)]
    candidates = [chunk(f"o{i}", OS) for i in range(5)]
    added = protected_source_topup(existing, candidates, reserved=3, protected_sources=PROTECTED)
    assert uids(added) == ["o0", "o1"]  # 1 already present -> need 2 more


def test_no_injection_when_reservation_already_met() -> None:
    existing = [chunk(f"o{i}", OS) for i in range(3)] + [chunk("s", SLACK)]
    candidates = [chunk("o_extra", OS)]
    assert protected_source_topup(existing, candidates, reserved=3, protected_sources=PROTECTED) == []


def test_skips_non_protected_and_dedupes_against_existing() -> None:
    existing = [chunk("dup", OS), chunk("s0", SLACK)]
    candidates = [
        chunk("dup", OS),       # already present -> skip
        chunk("s1", SLACK),     # not protected -> skip
        chunk("o_new", OS),     # inject
        chunk("w_new", WEB),    # protected (web) -> inject
    ]
    added = protected_source_topup(existing, candidates, reserved=3, protected_sources=PROTECTED)
    assert uids(added) == ["o_new", "w_new"]  # 1 present (dup) -> need 2


def test_disabled_when_reserved_zero_or_no_protected_sources() -> None:
    existing = [chunk("s", SLACK)]
    candidates = [chunk("o", OS)]
    assert protected_source_topup(existing, candidates, reserved=0, protected_sources=PROTECTED) == []
    assert protected_source_topup(existing, candidates, reserved=3, protected_sources=set()) == []
