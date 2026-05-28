"""Unit tests for ``danswer.db.persona_cache``.

What we lock down here:

1. **Filter parity vs SQL.** The Python filter in
   ``_filter_personas_for_user`` must match the OR-block in
   :func:`danswer.db.persona.get_personas` for every representative
   permission shape — public, direct-user grant, group grant, and the
   negative (none of the above). If either filter drifts, users see the
   wrong assistants. Each case here corresponds 1:1 to an SQL branch.

2. **Read path** with cache enabled:
   - Miss → DB call → Redis SET (with TTL)
   - Hit  → no DB call (the perf promise)
   - Per-user-groups miss/hit independently of the global personas miss/hit

3. **Read path** with cache disabled:
   - Always reads the DB; never touches Redis.
   - ``include_deleted=True`` always falls through to DB even when the
     cache is otherwise enabled — we deliberately don't cache that
     less-common shape.

4. **Invalidation:**
   - ``invalidate_personas_all`` deletes the right Redis key.
   - ``invalidate_user_groups(uid)`` deletes the per-user key.
   - Both short-circuit (no Redis call) when the cache is disabled —
     mutation paths shouldn't pay ambient cost in the off state.

5. **Fail-open** on Redis errors:
   - GET error → treated as miss → DB read → no crash.
   - SET / DELETE errors swallowed with a log; calling code sees nothing.

Redis is stubbed with a tiny in-memory fake; the inner DB function and
``PersonaSnapshot.from_model`` are patched. No real Postgres or Redis.
"""
from __future__ import annotations

import json
import unittest
import uuid
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

from danswer.db import persona_cache as pc


# ---------- shared fakes ----------


class _FakeRedis:
    """In-memory Redis fake covering get/set/delete only.

    Stores bytes the same way redis-py does so the cache module's
    JSON encode/decode actually runs in tests.
    """

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, Any, int | None]] = []
        self.delete_calls: list[str] = []

    def get(self, key: str) -> bytes | None:
        self.get_calls.append(key)
        return self.store.get(key)

    def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.set_calls.append((key, value, ex))
        if isinstance(value, str):
            self.store[key] = value.encode("utf-8")
        elif isinstance(value, bytes):
            self.store[key] = value
        else:
            self.store[key] = str(value).encode("utf-8")
        return True

    def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            self.delete_calls.append(k)
            if k in self.store:
                del self.store[k]
                removed += 1
        return removed


class _FakePersonaSnapshot:
    """Stand-in for ``PersonaSnapshot`` for filter tests only.

    The real Pydantic model has ~20 required fields; the filter function
    touches just three of them. We use a duck-typed mock so test cases
    stay focused on permission semantics, not Pydantic field plumbing.
    """

    def __init__(
        self,
        *,
        persona_id: int,
        is_public: bool,
        user_ids_with_access: list[uuid.UUID],
        group_ids_with_access: list[int],
    ) -> None:
        self.id = persona_id
        self.is_public = is_public
        # Match PersonaSnapshot.users: list[MinimalUserSnapshot] (has .id)
        self.users = [MagicMock(id=uid) for uid in user_ids_with_access]
        # Match PersonaSnapshot.groups: list[int]
        self.groups = group_ids_with_access


# ---------- filter parity ----------


class TestFilterParityVsSqlOrBlock(unittest.TestCase):
    """One test per SQL branch in get_personas's OR-filter.

    SQL (paraphrased):
        Persona.is_public
        OR Persona.id IN (Persona__User where user_id = U)
        OR Persona.id IN (Persona__UserGroup where group_id IN <U's groups>)

    Each case below isolates one branch; the last asserts the negative.
    """

    def setUp(self) -> None:
        self.user_id = uuid.uuid4()
        self.other_user_id = uuid.uuid4()
        self.user_group_ids = [10, 20]

    def test_public_persona_always_visible(self) -> None:
        """Branch 1: ``is_public`` → visible regardless of grants. This
        is the most-traveled path and must stay correct even when the
        user has no direct or group grant."""
        p = _FakePersonaSnapshot(
            persona_id=1, is_public=True, user_ids_with_access=[], group_ids_with_access=[]
        )
        result = pc._filter_personas_for_user([p], self.user_id, self.user_group_ids)
        self.assertEqual([x.id for x in result], [1])

    def test_direct_user_grant_visible(self) -> None:
        """Branch 2: not public, but the user is in the persona's
        ``users`` list. Mirrors a row in Persona__User."""
        p = _FakePersonaSnapshot(
            persona_id=2,
            is_public=False,
            user_ids_with_access=[self.user_id],
            group_ids_with_access=[],
        )
        result = pc._filter_personas_for_user([p], self.user_id, self.user_group_ids)
        self.assertEqual([x.id for x in result], [2])

    def test_group_grant_visible_if_user_in_one_of_those_groups(self) -> None:
        """Branch 3: not public, no direct grant, but a group the user
        belongs to has access. Mirrors a row in Persona__UserGroup
        joined with User__UserGroup."""
        p = _FakePersonaSnapshot(
            persona_id=3,
            is_public=False,
            user_ids_with_access=[],
            group_ids_with_access=[20, 999],  # 20 is one of the user's groups
        )
        result = pc._filter_personas_for_user([p], self.user_id, self.user_group_ids)
        self.assertEqual([x.id for x in result], [3])

    def test_no_access_hidden(self) -> None:
        """Negative: not public, not in users, no overlapping group →
        must be filtered out. If any branch leaks into this case we have
        a permission bug."""
        p = _FakePersonaSnapshot(
            persona_id=4,
            is_public=False,
            user_ids_with_access=[self.other_user_id],  # different user
            group_ids_with_access=[999, 888],  # no overlap with [10, 20]
        )
        result = pc._filter_personas_for_user([p], self.user_id, self.user_group_ids)
        self.assertEqual(result, [])

    def test_mixed_list_returns_only_visible(self) -> None:
        """A realistic mix: 4 personas, only the first 3 should pass
        the filter (one per branch + one denied). Verifies that the
        denial path doesn't accidentally short-circuit later visible
        items in the list."""
        personas = [
            _FakePersonaSnapshot(
                persona_id=1, is_public=True, user_ids_with_access=[],
                group_ids_with_access=[],
            ),
            _FakePersonaSnapshot(
                persona_id=2, is_public=False,
                user_ids_with_access=[self.user_id], group_ids_with_access=[],
            ),
            _FakePersonaSnapshot(
                persona_id=3, is_public=False, user_ids_with_access=[],
                group_ids_with_access=[10],
            ),
            _FakePersonaSnapshot(
                persona_id=4, is_public=False,
                user_ids_with_access=[self.other_user_id],
                group_ids_with_access=[888],
            ),
        ]
        result = pc._filter_personas_for_user(
            personas, self.user_id, self.user_group_ids
        )
        self.assertEqual(sorted(x.id for x in result), [1, 2, 3])

    def test_user_with_no_groups_still_sees_public_and_direct_grants(self) -> None:
        """Edge case: user belongs to zero groups. The group branch
        contributes nothing, but public + direct grants must still
        work — otherwise zero-group users get a broken assistant list."""
        personas = [
            _FakePersonaSnapshot(
                persona_id=1, is_public=True,
                user_ids_with_access=[], group_ids_with_access=[],
            ),
            _FakePersonaSnapshot(
                persona_id=2, is_public=False,
                user_ids_with_access=[self.user_id], group_ids_with_access=[],
            ),
            _FakePersonaSnapshot(
                persona_id=3, is_public=False,
                user_ids_with_access=[], group_ids_with_access=[10],
            ),
        ]
        result = pc._filter_personas_for_user(personas, self.user_id, [])
        self.assertEqual(sorted(x.id for x in result), [1, 2])


# ---------- read path ----------


class TestUserGroupCache(unittest.TestCase):
    """The per-user group-ids cache: cheap query, big aggregate win."""

    def test_miss_then_hit_only_one_db_read(self) -> None:
        """First call hits the DB, subsequent calls within TTL serve
        from Redis. Locks in the central performance promise of the
        per-user side of the cache."""
        fake = _FakeRedis()
        db_session = MagicMock()
        rows = MagicMock()
        rows.all.return_value = [10, 20, 30]
        db_session.scalars.return_value = rows
        user_id = uuid.uuid4()

        with patch.object(pc, "PERSONA_CACHE_ENABLED", True), patch.object(
            pc, "PERSONA_CACHE_TTL_SECONDS", 60
        ), patch.object(pc, "get_redis_client", return_value=fake):
            first = pc._get_user_group_ids_cached(user_id, db_session)
            second = pc._get_user_group_ids_cached(user_id, db_session)

        self.assertEqual(first, [10, 20, 30])
        self.assertEqual(second, [10, 20, 30])
        self.assertEqual(
            db_session.scalars.call_count,
            1,
            "second lookup must come from Redis, not the DB",
        )

    def test_set_uses_configured_ttl(self) -> None:
        """The TTL is the safety net for missed busts — if it isn't
        applied, a stale entry could live forever after a missed
        invalidation. Lock down that ``ex=`` is the configured value.
        """
        fake = _FakeRedis()
        db_session = MagicMock()
        rows = MagicMock()
        rows.all.return_value = []
        db_session.scalars.return_value = rows

        with patch.object(pc, "PERSONA_CACHE_ENABLED", True), patch.object(
            pc, "PERSONA_CACHE_TTL_SECONDS", 1234
        ), patch.object(pc, "get_redis_client", return_value=fake):
            pc._get_user_group_ids_cached(uuid.uuid4(), db_session)

        self.assertEqual(len(fake.set_calls), 1)
        _key, _val, ex = fake.set_calls[0]
        self.assertEqual(ex, 1234)


# ---------- routing / disabled mode ----------


class TestGetPersonasForUserCached(unittest.TestCase):
    def test_disabled_falls_through_to_get_personas(self) -> None:
        """With the flag off, the wrapper must NOT call Redis at all —
        it must behave exactly like the previous direct-DB code path.
        Important so enabling/disabling the feature is a clean toggle.
        """
        db_session = MagicMock()
        snap = MagicMock()

        with patch.object(pc, "PERSONA_CACHE_ENABLED", False), patch(
            "danswer.db.persona.get_personas", return_value=[MagicMock()]
        ) as mock_get_personas, patch(
            "danswer.db.persona_cache.PersonaSnapshot.from_model", return_value=snap
        ), patch.object(pc, "get_redis_client") as mock_client:
            result = pc.get_personas_for_user_cached(
                user_id=uuid.uuid4(), db_session=db_session
            )

        self.assertEqual(result, [snap])
        mock_get_personas.assert_called_once()
        mock_client.assert_not_called()

    def test_include_deleted_true_bypasses_cache_even_when_enabled(self) -> None:
        """We deliberately don't cache the ``include_deleted=True`` shape —
        keeps the cache key set small and avoids accidental mis-keying.
        Locks down that this path skips Redis entirely.
        """
        db_session = MagicMock()
        with patch.object(pc, "PERSONA_CACHE_ENABLED", True), patch(
            "danswer.db.persona.get_personas", return_value=[]
        ) as mock_get_personas, patch(
            "danswer.db.persona_cache.PersonaSnapshot.from_model"
        ), patch.object(pc, "get_redis_client") as mock_client:
            pc.get_personas_for_user_cached(
                user_id=uuid.uuid4(),
                db_session=db_session,
                include_deleted=True,
            )

        mock_get_personas.assert_called_once()
        # include_deleted=True was passed through to the DB read
        self.assertTrue(mock_get_personas.call_args.kwargs["include_deleted"])
        mock_client.assert_not_called()

    def test_admin_call_returns_unfiltered_global_cache(self) -> None:
        """``user_id=None`` is the admin / no-auth case. The cache
        already holds the full list with no permission filter, so we
        skip the Python filter step. Locks down the fast path for
        admin endpoints that share the same cache.
        """
        all_snaps = [
            _FakePersonaSnapshot(
                persona_id=i,
                is_public=False,
                user_ids_with_access=[],
                group_ids_with_access=[],
            )
            for i in [1, 2, 3]
        ]
        with patch.object(pc, "PERSONA_CACHE_ENABLED", True), patch.object(
            pc, "_get_all_personas_cached", return_value=all_snaps
        ) as mock_get_all, patch.object(
            pc, "_get_user_group_ids_cached"
        ) as mock_get_groups:
            result = pc.get_personas_for_user_cached(
                user_id=None, db_session=MagicMock()
            )

        self.assertEqual([x.id for x in result], [1, 2, 3])
        mock_get_all.assert_called_once()
        # Critically, we did NOT look up groups for the admin path.
        mock_get_groups.assert_not_called()


# ---------- invalidation ----------


class TestInvalidation(unittest.TestCase):
    def test_invalidate_personas_all_deletes_right_key(self) -> None:
        """The bust call must target ``personas:all:not_deleted``. Any
        drift between this key and the SET key in the read path would
        produce a stuck cache."""
        fake = _FakeRedis()
        with patch.object(pc, "PERSONA_CACHE_ENABLED", True), patch.object(
            pc, "get_redis_client", return_value=fake
        ):
            pc.invalidate_personas_all()
        self.assertIn("danswer:personas:all:not_deleted", fake.delete_calls)

    def test_invalidate_user_groups_deletes_per_user_key(self) -> None:
        """Each user gets their own key. The bust must include the
        user_id in string form (UUIDs are not JSON-stringified
        consistently otherwise)."""
        fake = _FakeRedis()
        uid = uuid.uuid4()
        with patch.object(pc, "PERSONA_CACHE_ENABLED", True), patch.object(
            pc, "get_redis_client", return_value=fake
        ):
            pc.invalidate_user_groups(uid)
        self.assertIn(f"danswer:personas:groups:{uid}", fake.delete_calls)

    def test_invalidate_when_disabled_short_circuits(self) -> None:
        """When the flag is off, mutation paths must not pay a Redis
        round-trip cost. Without this, every assistant edit would touch
        Redis even on a deployment that's opted out."""
        with patch.object(pc, "PERSONA_CACHE_ENABLED", False), patch.object(
            pc, "get_redis_client"
        ) as mock_client:
            pc.invalidate_personas_all()
            pc.invalidate_user_groups(uuid.uuid4())
        mock_client.assert_not_called()

    def test_redis_error_during_bust_is_swallowed(self) -> None:
        """If the bust call fails (Redis down, network blip), we don't
        want to roll back the user's mutation — the DB write already
        committed. Loud log, no exception."""
        bad = MagicMock()
        bad.delete.side_effect = RuntimeError("redis exploded")
        with patch.object(pc, "PERSONA_CACHE_ENABLED", True), patch.object(
            pc, "get_redis_client", return_value=bad
        ):
            # Both must complete without raising.
            pc.invalidate_personas_all()
            pc.invalidate_user_groups(uuid.uuid4())


# ---------- fail-open on read ----------


class TestFailOpenOnRedisRead(unittest.TestCase):
    def test_redis_get_error_treated_as_miss(self) -> None:
        """Redis GET exploding (timeout, conn refused) must NOT
        propagate — the wrapper falls through to a direct DB read so
        a Redis outage degrades latency, not availability.

        We stub _safe_set out: the set path's round-trip serialization
        (s.json() → json.loads) requires a real PersonaSnapshot. Here
        we're verifying the GET-error fallback, not the SET path.
        """
        bad = MagicMock()
        bad.get.side_effect = RuntimeError("connection refused")
        db_session = MagicMock()
        snap = MagicMock(is_public=True, users=[], groups=[])
        # The cache module round-trips via json.loads(s.json()) before
        # the SET — needs a real JSON string here.
        snap.json.return_value = '{"id":1,"is_public":true}'

        with patch.object(pc, "PERSONA_CACHE_ENABLED", True), patch.object(
            pc, "get_redis_client", return_value=bad
        ), patch(
            "danswer.db.persona.get_personas", return_value=[MagicMock()]
        ), patch(
            "danswer.db.persona_cache.PersonaSnapshot.from_model", return_value=snap
        ), patch.object(pc, "_safe_set"):
            # Must not raise; must return the DB result.
            result = pc._get_all_personas_cached(db_session)

        self.assertEqual(result, [snap])

    def test_corrupt_cache_entry_treated_as_miss(self) -> None:
        """Non-JSON bytes under our key (legacy format, manual SET,
        schema migration race) must not crash. Fall through to DB and
        overwrite the corrupt entry on the next SET. (Same _safe_set
        stub rationale as above.)
        """
        fake = _FakeRedis()
        fake.store["danswer:personas:all:not_deleted"] = b"not-json-at-all"
        db_session = MagicMock()
        snap = MagicMock(is_public=True, users=[], groups=[])
        # The cache module round-trips via json.loads(s.json()) before
        # the SET — needs a real JSON string here.
        snap.json.return_value = '{"id":1,"is_public":true}'

        with patch.object(pc, "PERSONA_CACHE_ENABLED", True), patch.object(
            pc, "get_redis_client", return_value=fake
        ), patch(
            "danswer.db.persona.get_personas", return_value=[MagicMock()]
        ), patch(
            "danswer.db.persona_cache.PersonaSnapshot.from_model", return_value=snap
        ), patch.object(pc, "_safe_set"):
            result = pc._get_all_personas_cached(db_session)

        self.assertEqual(result, [snap])


if __name__ == "__main__":
    unittest.main()
