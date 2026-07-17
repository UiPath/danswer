"""Unit tests for the background onboarding finalizer state machine.

Covers `_source_index_state` (reading per-source scrape status, incl. the
in-progress flag) and `finalize_ready_onboarding_requests` (finalize on success,
flag failure once, recover a previously-FAILED request, and flip a re-indexing
FAILED request back to INDEXING) — positive and negative paths."""
from types import SimpleNamespace
from typing import Any

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
    all_indexed, failures, in_progress = provision._source_index_state(req, sess)  # type: ignore[arg-type]
    assert all_indexed is True
    assert failures == []
    assert in_progress is False


def test_state_one_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cc(monkeypatch)
    req = SimpleNamespace(cc_pair_ids=[10, 11])
    sess = _Session(
        [_attempt(IndexingStatus.SUCCESS), _attempt(IndexingStatus.FAILED, "boom")]
    )
    all_indexed, failures, in_progress = provision._source_index_state(req, sess)  # type: ignore[arg-type]
    assert all_indexed is False
    assert len(failures) == 1 and "boom" in failures[0]
    assert in_progress is False


def test_state_in_progress_flag_when_running(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cc(monkeypatch)
    req = SimpleNamespace(cc_pair_ids=[10, 11])
    sess = _Session(
        [_attempt(IndexingStatus.SUCCESS), _attempt(IndexingStatus.IN_PROGRESS)]
    )
    all_indexed, failures, in_progress = provision._source_index_state(req, sess)  # type: ignore[arg-type]
    assert all_indexed is False  # still scraping
    assert failures == []  # not a failure either
    assert in_progress is True


def test_state_in_progress_flag_when_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A freshly-queued (NOT_STARTED) attempt also counts as in-progress: it's
    # what a just-triggered re-index looks like before a worker picks it up.
    _patch_cc(monkeypatch)
    req = SimpleNamespace(cc_pair_ids=[10])
    sess = _Session([_attempt(IndexingStatus.NOT_STARTED)])
    all_indexed, failures, in_progress = provision._source_index_state(req, sess)  # type: ignore[arg-type]
    assert all_indexed is False
    assert failures == []
    assert in_progress is True


def test_state_no_attempt_yet_is_not_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No attempt row at all (None) is neither done nor actively running.
    _patch_cc(monkeypatch)
    req = SimpleNamespace(cc_pair_ids=[10])
    sess = _Session([None])  # no index attempt row yet
    all_indexed, failures, in_progress = provision._source_index_state(req, sess)  # type: ignore[arg-type]
    assert all_indexed is False
    assert failures == []
    assert in_progress is False


def test_state_no_cc_pairs() -> None:
    req = SimpleNamespace(cc_pair_ids=[])
    assert provision._source_index_state(req, None) == (  # type: ignore[arg-type]
        False,
        [],
        False,
    )


# --- finalize_ready_onboarding_requests ------------------------------------


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    indexing: list,
    failed: list,
    state: Any,  # callable(request) -> (all_indexed, failures, in_progress)
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
    calls = _wire(
        monkeypatch, indexing=[req], failed=[], state=lambda r: (True, [], False)
    )
    provision.finalize_ready_onboarding_requests(None)  # type: ignore[arg-type]
    assert calls["finalized"] == [1]
    assert calls["failed_notified"] == []


def test_flags_failure_once(monkeypatch: pytest.MonkeyPatch) -> None:
    req = _req(2, OnboardingStatus.INDEXING)
    calls = _wire(
        monkeypatch,
        indexing=[req],
        failed=[],
        state=lambda r: (False, ["src-1: boom"], False),
    )
    provision.finalize_ready_onboarding_requests(None)  # type: ignore[arg-type]
    assert (2, OnboardingStatus.FAILED.value) in calls["status"]
    assert calls["failed_notified"] == [(2, "src-1: boom")]
    assert calls["finalized"] == []


def test_already_failed_not_renotified(monkeypatch: pytest.MonkeyPatch) -> None:
    # A FAILED request that is still failing (nothing re-indexing) must not spam
    # notifications and must stay FAILED.
    req = _req(3, OnboardingStatus.FAILED)
    calls = _wire(
        monkeypatch,
        indexing=[],
        failed=[req],
        state=lambda r: (False, ["still bad"], False),
    )
    provision.finalize_ready_onboarding_requests(None)  # type: ignore[arg-type]
    assert calls["failed_notified"] == []
    assert calls["finalized"] == []
    assert calls["status"] == []  # no transition


def test_recovers_previously_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A FAILED request whose sources now all succeed is finalized — no restart.
    req = _req(4, OnboardingStatus.FAILED)
    calls = _wire(
        monkeypatch, indexing=[], failed=[req], state=lambda r: (True, [], False)
    )
    provision.finalize_ready_onboarding_requests(None)  # type: ignore[arg-type]
    assert calls["finalized"] == [4]


def test_failed_reindexing_flips_back_to_indexing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A FAILED request that is now re-indexing (sources fixed + re-run) with
    # nothing currently failing flips back to INDEXING so the requester sees
    # progress — not finalized yet, not re-notified.
    req = _req(8, OnboardingStatus.FAILED)
    calls = _wire(
        monkeypatch, indexing=[], failed=[req], state=lambda r: (False, [], True)
    )
    provision.finalize_ready_onboarding_requests(None)  # type: ignore[arg-type]
    assert (8, OnboardingStatus.INDEXING.value) in calls["status"]
    assert calls["finalized"] == []
    assert calls["failed_notified"] == []


def test_failed_partial_reindex_flips_to_indexing_despite_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # In-progress WINS over a failure: a FAILED request with a source still
    # scraping flips to INDEXING (work is happening) even though another source's
    # latest attempt is failed — no re-notify. The failure verdict is deferred
    # until everything settles.
    req = _req(9, OnboardingStatus.FAILED)
    calls = _wire(
        monkeypatch,
        indexing=[],
        failed=[req],
        state=lambda r: (False, ["src-2: still bad"], True),
    )
    provision.finalize_ready_onboarding_requests(None)  # type: ignore[arg-type]
    assert (9, OnboardingStatus.INDEXING.value) in calls["status"]
    assert calls["failed_notified"] == []
    assert calls["finalized"] == []


def test_indexing_with_a_failure_but_still_running_stays_indexing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A source failed while a sibling is still scraping: don't prematurely flag
    # FAILED — stay INDEXING and wait for everything to settle.
    req = _req(10, OnboardingStatus.INDEXING)
    calls = _wire(
        monkeypatch,
        indexing=[req],
        failed=[],
        state=lambda r: (False, ["src-1: boom"], True),
    )
    provision.finalize_ready_onboarding_requests(None)  # type: ignore[arg-type]
    assert calls["status"] == []  # already INDEXING, no flip needed
    assert calls["failed_notified"] == []  # not flagged failed yet
    assert calls["finalized"] == []


def test_still_indexing_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    req = _req(5, OnboardingStatus.INDEXING)
    calls = _wire(
        monkeypatch, indexing=[req], failed=[], state=lambda r: (False, [], True)
    )
    provision.finalize_ready_onboarding_requests(None)  # type: ignore[arg-type]
    assert calls["finalized"] == []
    assert calls["failed_notified"] == []
    assert calls["status"] == []


# --- finalize_onboarding: idempotent, crash-safe artifact creation ----------


class _FakeSession:
    """Records rollback so we can assert the read txn is dropped before begin()."""

    def __init__(self, order: list) -> None:
        self._order = order

    def rollback(self) -> None:
        self._order.append("rollback")

    def commit(self) -> None:
        pass


def _finalize_request(**over: object) -> SimpleNamespace:
    base: dict = dict(
        id=1,
        payload={"team_name": "T", "sources": [], "response_type": "citations"},
        cc_pair_ids=[1, 2],
        approver_id=1,
        requester_id=2,
        document_set_id=None,
        persona_id=None,
        slack_bot_config_id=None,
        status=OnboardingStatus.INDEXING.value,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _wire_finalize(
    monkeypatch: pytest.MonkeyPatch,
    *,
    order: list,
    fail_on: str | None = None,
    notify_raises: bool = False,
) -> dict:
    """Patch finalize_onboarding's collaborators. Records step order; can inject a
    failure at a given step; set_provisioned_ids writes ids back onto the request
    so a follow-up call sees the resumed (checkpointed) state."""
    calls: dict = {"notified": 0}
    monkeypatch.setattr(
        provision, "_finalize_owner", lambda r, db: SimpleNamespace(id=9)
    )

    def _set_ids(db: object, req: object, **kw: object) -> object:
        for k, v in kw.items():
            setattr(req, k, v)
        return req

    monkeypatch.setattr(provision, "set_provisioned_ids", _set_ids)

    def _doc(req: object, uid: object, db: object) -> tuple:
        order.append("doc_set")
        if fail_on == "doc_set":
            raise RuntimeError("boom doc_set")
        return SimpleNamespace(id=101), []

    monkeypatch.setattr(provision, "insert_document_set", _doc)
    monkeypatch.setattr(
        provision, "_build_prompt", lambda p, o, db: SimpleNamespace(id=2)
    )

    def _persona(**kw: object) -> object:
        order.append("persona")
        if fail_on == "persona":
            raise RuntimeError("boom persona")
        return SimpleNamespace(id=202)

    monkeypatch.setattr(provision, "upsert_persona", _persona)
    monkeypatch.setattr(provision, "_build_channel_config", lambda p, ps: {})
    monkeypatch.setattr(provision, "_prioritized_sources", lambda s: [])

    def _slack(**kw: object) -> object:
        order.append("slack")
        if fail_on == "slack":
            raise RuntimeError("boom slack")
        return SimpleNamespace(id=303)

    monkeypatch.setattr(provision, "insert_slack_bot_config", _slack)

    def _status(db: object, req: object, status: object, **kw: object) -> object:
        req.status = status.value  # type: ignore[attr-defined]
        order.append(f"status:{status.value}")  # type: ignore[attr-defined]
        return req

    monkeypatch.setattr(provision, "update_onboarding_status", _status)

    def _notify(req: object) -> None:
        calls["notified"] += 1
        if notify_raises:
            raise RuntimeError("slack down")

    monkeypatch.setattr(provision, "notify_onboarding_complete", _notify)
    return calls


def test_finalize_creates_all_artifacts_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Happy path: rollback first (so insert_document_set can begin() its own
    # transaction), then doc set -> persona -> slack -> COMPLETE, notify once.
    order: list[str] = []
    calls = _wire_finalize(monkeypatch, order=order)
    req = _finalize_request()
    provision.finalize_onboarding(req, _FakeSession(order))  # type: ignore[arg-type]
    assert order == ["rollback", "doc_set", "persona", "slack", "status:complete"]
    assert req.document_set_id == 101
    assert req.persona_id == 202
    assert req.slack_bot_config_id == 303
    assert req.status == OnboardingStatus.COMPLETE.value
    assert calls["notified"] == 1


def test_finalize_drops_read_txn_before_document_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: insert_document_set calls db_session.begin(), which raises if a
    # transaction is already active (the finalizer opened one doing its reads).
    # The rollback must happen before the document set is created.
    order: list[str] = []
    _wire_finalize(monkeypatch, order=order)
    provision.finalize_onboarding(_finalize_request(), _FakeSession(order))  # type: ignore[arg-type]
    assert order.index("rollback") < order.index("doc_set")


def test_finalize_skips_document_set_when_already_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Idempotent: a request that already has a document_set_id must not create a
    # second one (the duplicate-artifact bug a pod crash would otherwise cause).
    order: list[str] = []
    _wire_finalize(monkeypatch, order=order)
    req = _finalize_request(document_set_id=999)
    provision.finalize_onboarding(req, _FakeSession(order))  # type: ignore[arg-type]
    assert "doc_set" not in order
    assert order == ["rollback", "persona", "slack", "status:complete"]
    assert req.document_set_id == 999  # unchanged


def test_finalize_resumes_only_remaining_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Resume: doc set + persona already done -> only the Slack config is created.
    order: list[str] = []
    _wire_finalize(monkeypatch, order=order)
    req = _finalize_request(document_set_id=999, persona_id=888)
    provision.finalize_onboarding(req, _FakeSession(order))  # type: ignore[arg-type]
    assert order == ["rollback", "slack", "status:complete"]
    assert req.slack_bot_config_id == 303


def test_finalize_recovers_after_crash_without_duplicating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Crash-safety: the first attempt creates doc set + persona (ids persisted)
    # then dies at the Slack step. The retry must NOT recreate doc set / persona
    # — it resumes at Slack and completes.
    order1: list[str] = []
    _wire_finalize(monkeypatch, order=order1, fail_on="slack")
    req = _finalize_request()
    with pytest.raises(RuntimeError):
        provision.finalize_onboarding(req, _FakeSession(order1))  # type: ignore[arg-type]
    # Checkpoints persisted before the crash:
    assert req.document_set_id == 101
    assert req.persona_id == 202
    assert req.slack_bot_config_id is None
    assert req.status != OnboardingStatus.COMPLETE.value

    # Next finalizer tick (no injected failure): resumes at Slack only.
    order2: list[str] = []
    calls2 = _wire_finalize(monkeypatch, order=order2)
    provision.finalize_onboarding(req, _FakeSession(order2))  # type: ignore[arg-type]
    assert order2 == ["rollback", "slack", "status:complete"]
    assert "doc_set" not in order2 and "persona" not in order2
    assert req.slack_bot_config_id == 303
    assert req.status == OnboardingStatus.COMPLETE.value
    assert calls2["notified"] == 1


def test_finalize_completes_even_if_notify_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A completion-notification failure must not fail finalize — the request is
    # already COMPLETE and would otherwise be retried (and re-notified) forever.
    order: list[str] = []
    calls = _wire_finalize(monkeypatch, order=order, notify_raises=True)
    req = _finalize_request()
    provision.finalize_onboarding(req, _FakeSession(order))  # type: ignore[arg-type]
    assert req.status == OnboardingStatus.COMPLETE.value
    assert calls["notified"] == 1


def test_finalizer_isolates_finalize_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If finalize_onboarding throws, the sweep must swallow it (rollback + log)
    # so the request stays tracked for the next tick and other requests proceed.
    req = _req(12, OnboardingStatus.INDEXING)
    calls = _wire(
        monkeypatch, indexing=[req], failed=[], state=lambda r: (True, [], False)
    )

    def _boom(r: object, db: object) -> None:
        raise RuntimeError("finalize kaboom")

    monkeypatch.setattr(provision, "finalize_onboarding", _boom)
    db = SimpleNamespace(rollback=lambda: calls.setdefault("rolled_back", True))
    # Must not raise.
    provision.finalize_ready_onboarding_requests(db)  # type: ignore[arg-type]
    assert calls.get("rolled_back") is True


def test_per_request_error_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    # One request blowing up must not stop the others from being processed.
    good = _req(6, OnboardingStatus.INDEXING)
    bad = _req(7, OnboardingStatus.INDEXING)

    def _state(r: object) -> tuple:
        if r.id == 7:  # type: ignore[attr-defined]
            raise RuntimeError("kaboom")
        return (True, [], False)

    calls = _wire(monkeypatch, indexing=[bad, good], failed=[], state=_state)
    db = SimpleNamespace(rollback=lambda: calls.setdefault("rolled_back", True))
    provision.finalize_ready_onboarding_requests(db)  # type: ignore[arg-type]
    assert calls["finalized"] == [6]  # good one still finalized
    assert calls.get("rolled_back") is True  # bad one rolled back
