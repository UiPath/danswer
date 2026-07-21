"""Unit tests for the serialization-failure detector used to retry indexing
batches on Postgres 40001 under concurrent load."""
from danswer.background.indexing.run_indexing import _is_serialization_failure


class _Orig:
    def __init__(self, pgcode: str | None) -> None:
        self.pgcode = pgcode


class _OpErr(Exception):
    """Stand-in for sqlalchemy.exc.OperationalError (carries .orig)."""

    def __init__(self, orig: object) -> None:
        self.orig = orig


def test_detects_40001_serialization_failure() -> None:
    assert _is_serialization_failure(_OpErr(_Orig("40001"))) is True


def test_ignores_other_sqlstates_and_shapes() -> None:
    assert _is_serialization_failure(_OpErr(_Orig("23505"))) is False  # unique viol
    assert _is_serialization_failure(_OpErr(_Orig(None))) is False
    assert _is_serialization_failure(_OpErr(None)) is False
    assert _is_serialization_failure(RuntimeError("not a db error")) is False
