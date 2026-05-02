"""Unit tests for the per-cc-pair deletion advisory-lock key derivation
(``danswer.db.connector_credential_pair._deletion_lock_key``).

Lock acquisition itself requires a real Postgres ``pg_try_advisory_lock``
and is exercised by the scheduler/deletion e2e — here we only verify the
pure key math: stable, deterministic, namespaced away from the indexing
lock, and within Postgres ``bigint`` range.
"""
from __future__ import annotations

import unittest

from danswer.db.connector_credential_pair import _deletion_lock_key
from danswer.db.connector_credential_pair import _DELETION_LOCK_KEY_OFFSET
from danswer.db.index_attempt import _cc_pair_lock_key
from danswer.db.index_attempt import _INDEXING_LOCK_KEY_OFFSET


_PG_BIGINT_MIN = -(1 << 63)
_PG_BIGINT_MAX = (1 << 63) - 1


class TestDeletionLockKey(unittest.TestCase):
    def test_namespace_distinct_from_indexing_lock(self) -> None:
        """Deletion namespace prefix must NOT equal indexing prefix.

        If they collided, an active indexing run would block a deletion
        sweep (and vice versa) for *unrelated* cc-pairs whose hashes
        happen to match. The whole point of separate prefixes is that
        the two locks live in disjoint regions of the 64-bit key space.
        """
        self.assertNotEqual(_DELETION_LOCK_KEY_OFFSET, _INDEXING_LOCK_KEY_OFFSET)

    def test_same_cc_pair_distinct_keys_across_namespaces(self) -> None:
        """For the same (connector_id, credential_id), the indexing lock
        and the deletion lock must produce different keys — otherwise
        an indexing run on cc-pair X would block deletion of cc-pair X
        without us ever holding the deletion lock."""
        for cid, credid in [(1, 1), (7, 5), (111, 5), (12345, 67890)]:
            idx_key = _cc_pair_lock_key(cid, credid)
            del_key = _deletion_lock_key(cid, credid)
            self.assertNotEqual(
                idx_key,
                del_key,
                f"keys collided for cc-pair=({cid},{credid}): "
                f"indexing={idx_key} deletion={del_key}",
            )

    def test_deterministic(self) -> None:
        """Same inputs → same key, every call. No randomness, no
        process-state dependency."""
        for cid, credid in [(0, 0), (1, 2), (999, 1), (12345, 67890)]:
            self.assertEqual(
                _deletion_lock_key(cid, credid),
                _deletion_lock_key(cid, credid),
            )

    def test_distinct_cc_pairs_distinct_keys(self) -> None:
        """Different cc-pairs should produce different keys (collisions
        possible at the 32-bit hash level — birthday bound at ~65k
        cc-pairs is below 1% — but a small smoke set must not collide)."""
        seen: dict[int, tuple[int, int]] = {}
        for cid in range(1, 25):
            for credid in range(1, 25):
                k = _deletion_lock_key(cid, credid)
                if k in seen:
                    self.fail(
                        f"hash collision in small key space: "
                        f"({cid},{credid}) and {seen[k]} both → {k}"
                    )
                seen[k] = (cid, credid)

    def test_within_postgres_bigint_range(self) -> None:
        """Postgres advisory-lock keys are signed 64-bit (bigint).
        Anything outside that range raises a runtime error from the
        driver. Sweep a large input space to catch any sign-extension
        or wraparound bug in the key math."""
        for cid in range(1, 5000, 137):
            for credid in range(1, 5000, 211):
                k = _deletion_lock_key(cid, credid)
                self.assertGreaterEqual(k, _PG_BIGINT_MIN)
                self.assertLessEqual(k, _PG_BIGINT_MAX)

    def test_namespace_offset_in_high_bits(self) -> None:
        """The high 32 bits of the key (before sign-extension) should
        reflect the deletion namespace (b\"DELE\")."""
        # Pick an input whose hash low bits are 0 so the high bits are
        # the offset bits unmodified.
        # The hash is `(cid * 0x9E3779B1 ^ credid) & 0xFFFFFFFF`. With
        # cid=0, credid=0 the hash is 0 → key == offset.
        self.assertEqual(_deletion_lock_key(0, 0), _DELETION_LOCK_KEY_OFFSET)


if __name__ == "__main__":
    unittest.main()
