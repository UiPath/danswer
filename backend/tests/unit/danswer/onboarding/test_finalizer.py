"""Unit tests for the background onboarding finalizer state machine.

Covers `_source_index_state` (reading per-source scrape status) and
`finalize_ready_onboarding_requests` (finalize on success, flag failure once,
recover a previously-FAILED request) — positive and negative paths."""
from types import SimpleNamespace

import pytest

from danswer.db.enums import IndexingStatus
from danswer.db.models import OnboardingStatus
from danswer.onboarding import provision


# --- _source_index_state ---------------------------------------------------


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _Session:
    """Returns queued latest-attempt rows, one per execute() call (one per cc)."""

    def __init__(self, attempts: list) -> None:
        self._attempts = list(attempts)

    def execute(self, *args: object, **kwargs: object) -> _Result:
        return _Result(self._attempts.pop(0) if self._attempts else None)


def _attempt(status: IndexingStatus, err: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(status=status, error_msg=err)


def _patch_cc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provision,
        "get_connector_credential_pair_from_id",
        lambda cc_id, db: SimpleNamespace(
            connector_id=cc_id, credential_id=cc_id, name=f"src-{cc_id}"
        ),
    )


def test_state_all_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cc(monkeypatch)
    req = SimpleNamespace(cc_pair_ids=[10, 11])
    sess = _Session(
        [_attempt(IndexingStatus.SUCCESS), _attempt(IndexingStatus.SUCCESS)]
    )
    all_indexed, failures = provision._source_index_state(req, sess)  # type: ignore[arg-type]
    assert all_indexed is True
    assert failures == []


def test_state_one_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cc(monkeypatch)
    req = SimpleNamespace(cc_pair_ids=[10, 11])
    sess = _Session(
        [_attempt(IndexingStatus.SUCCESS), _attempt(IndexingStatus.FAILED, "boom")]
    )
    all_indexed, failures = provision._source_index_state(req, sess)  # type: ignore[arg-type]
    assert all_indexed is False
    assert len(failures) == 1 and "boom" in failures[0]


def test_state_in_progress_is_not_done(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cc(monkeypatch)
    req = SimpleNamespace(cc_pair_ids=[10, 11])
    sess = _Session(
        [_attempt(IndexingStatus.SUCCESS), _attempt(IndexingStatus.IN_PROGRESS)]
    )
    all_indexed, failures = provision._source_index_state(req, sess)  # type: ignore[arg-type]
    assert all_indexed is False  # still scraping
    assert failures == []  # not a failure either


def test_state_no_attempt_yet_is_not_done(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cc(monkeypatch)
    req = SimpleNamespace(cc_pair_ids=[10])
    sess = _Session([None])  # no index attempt row yet
    all_indexed, failures = provision._source_index_state(req, sess)  # type: ignore[arg-type]
    assert all_indexed is False
    assert failures == []


def test_state_no_cc_pairs() -> None:
    req = SimpleNamespace(cc_pair_ids=[])
    assert provision._source_index_state(req, None) == (False, [])  # type: ignore[arg-type]


# --- finalize_ready_onboarding_requests ------------------------------------


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    indexing: list,
    failed: list,
    state,  # callable(request) -> (all_indexed, failures)
) -> dict:
    """Patch the finalizer's collaborators and capture what it does."""
    calls: dict = {"finalized": [], "failed_notified": [], "status": []}

    def _list(db: object, status: OnboardingStatus) -> list:
        return indexing if status == OnboardingStatus.INDEXING else failed

    monkeypatch.setattr(provision, "list_onboarding_requests", _list)
    monkeypatch.setattr(provision, "_source_index_state", lambda r, db: state(r))
    monkeypatch.setattr(
        provision, "finalize_onboarding", lambda r, db: calls["finalized"].append(r.id)
    )
    monkeypatch.setattr(
        provision,
        "notify_onboarding_failed",
        lambda r, detail: calls["failed_notified"].append((r.id, detail)),
    )

    def _update(
        db: object, r: object, status: OnboardingStatus, **kw: object
    ) -> object:
        r.status = status.value  # type: ignore[attr-defined]
        calls["status"].append((r.id, status.value))  # type: ignore[attr-defined]
        return r

    monkeypatch.setattr(provision, "update_onboarding_status", _update)
    return calls


def _req(id_: int, status: OnboardingStatus) -> SimpleNamespace:
    return SimpleNamespace(id=id_, status=status.value, cc_pair_ids=[1])


def test_finalizes_when_all_indexed(monkeypatch: pytest.MonkeyPatch) -> None:
    req = _req(1, OnboardingStatus.INDEXING)
    calls = _wire(monkeypatch, indexing=[req], failed=[], state=lambda r: (True, []))
    provision.finalize_ready_onboarding_requests(None)  # type: ignore[arg-type]
    assert calls["finalized"] == [1]
    assert calls["failed_notified"] == []


def test_flags_failure_once(monkeypatch: pytest.MonkeyPatch) -> None:
    req = _req(2, OnboardingStatus.INDEXING)
    calls = _wire(
        monkeypatch, indexing=[req], failed=[], state=lambda r: (False, ["src-1: boom"])
    )
    provision.finalize_ready_onboarding_requests(None)  # type: ignore[arg-type]
    assert (2, OnboardingStatus.FAILED.value) in calls["status"]
    assert calls["failed_notified"] == [(2, "src-1: boom")]
    assert calls["finalized"] == []


def test_already_failed_not_renotified(monkeypatch: pytest.MonkeyPatch) -> None:
    # A FAILED request that is still failing must not spam notifications.
    req = _req(3, OnboardingStatus.FAILED)
    calls = _wire(
        monkeypatch, indexing=[], failed=[req], state=lambda r: (False, ["still bad"])
    )
    provision.finalize_ready_onboarding_requests(None)  # type: ignore[arg-type]
    assert calls["failed_notified"] == []
    assert calls["finalized"] == []


def test_recovers_previously_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A FAILED request whose sources now succeed is finalized — no restart needed.
    req = _req(4, OnboardingStatus.FAILED)
    calls = _wire(monkeypatch, indexing=[], failed=[req], state=lambda r: (True, []))
    provision.finalize_ready_onboarding_requests(None)  # type: ignore[arg-type]
    assert calls["finalized"] == [4]


def test_still_indexing_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    req = _req(5, OnboardingStatus.INDEXING)
    calls = _wire(monkeypatch, indexing=[req], failed=[], state=lambda r: (False, []))
    provision.finalize_ready_onboarding_requests(None)  # type: ignore[arg-type]
    assert calls["finalized"] == []
    assert calls["failed_notified"] == []


def test_per_request_error_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    # One request blowing up must not stop the others from being processed.
    good = _req(6, OnboardingStatus.INDEXING)
    bad = _req(7, OnboardingStatus.INDEXING)

    def _state(r: object) -> tuple:
        if r.id == 7:  # type: ignore[attr-defined]
            raise RuntimeError("kaboom")
        return (True, [])

    calls = _wire(monkeypatch, indexing=[bad, good], failed=[], state=_state)
    db = SimpleNamespace(rollback=lambda: calls.setdefault("rolled_back", True))
    provision.finalize_ready_onboarding_requests(db)  # type: ignore[arg-type]
    assert calls["finalized"] == [6]  # good one still finalized
    assert calls.get("rolled_back") is True  # bad one rolled back
