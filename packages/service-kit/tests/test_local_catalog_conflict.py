"""A lost commit race on the IN-PROCESS catalog write path must be a 409, not a 500 (SK-02).

`LocalCatalogWriteTransport` is the write path `open_writer` selects when
`write_backend=='catalog'` and no `catalog_uri` is set — the native catalog behaviour over a
local `lance.LanceDataset`. Its merge/delete steps hit exactly the same optimistic-concurrency
window as the direct `LanceTableWriter`: the caller reads a version, builds a delta, then commits,
and a writer that lands in between loses the commit as a raw `OSError`.

`LanceTableWriter` wraps all three ops in `translate_commit_conflict()`, but the in-process catalog
transport did not — so the same lost race that is a recoverable 409 on the direct path escaped as an
unmapped `OSError` (a 500) here. These pin that the two write paths translate the race identically,
and that an unrelated `OSError` still propagates untouched.
"""

from __future__ import annotations

from typing import cast

import lance
import pyarrow as pa
import pytest

from service_kit.exceptions import ConflictError
from service_kit.lancekit.writer import LocalCatalogWriteTransport


class _RaisingMerge:
    def __init__(self, exc: OSError) -> None:
        self._exc = exc

    def when_matched_update_all(self) -> _RaisingMerge:
        return self

    def when_not_matched_insert_all(self) -> _RaisingMerge:
        return self

    def execute(self, _delta: pa.Table) -> None:
        raise self._exc


class _FakeDataset:
    """Stands in for `lance.LanceDataset`: its merge/delete steps raise the OSError a lost
    commit race surfaces as, so the test observes only how the transport TRANSLATES it."""

    def __init__(self, exc: OSError) -> None:
        self._exc = exc

    def merge_insert(self, _on: str) -> _RaisingMerge:
        return _RaisingMerge(self._exc)

    def delete(self, _predicate: str) -> None:
        raise self._exc


def _transport(exc: OSError) -> LocalCatalogWriteTransport:
    # The transport only calls merge_insert(...).execute / delete on its dataset; a fake that
    # raises there is enough to observe the translation, and cast keeps the constructor's
    # `lance.LanceDataset` contract honest without a real on-disk dataset.
    return LocalCatalogWriteTransport(cast(lance.LanceDataset, _FakeDataset(exc)))


_DELTA = pa.table({"id": [1]})


def test_merge_upsert_lost_race_becomes_a_conflict() -> None:
    with pytest.raises(ConflictError):
        _transport(OSError("Commit conflict for version 8")).merge_upsert(_DELTA, "id")


def test_merge_insert_only_lost_race_becomes_a_conflict() -> None:
    with pytest.raises(ConflictError):
        _transport(OSError("Concurrent write detected")).merge_insert_only(_DELTA, "id")


def test_delete_lost_race_becomes_a_conflict() -> None:
    with pytest.raises(ConflictError):
        _transport(OSError("commit conflict: retry")).delete("id = 1")


def test_an_unrelated_oserror_still_propagates() -> None:
    """The dangerous direction: an outage must not be laundered into a routine-looking 409."""
    with pytest.raises(OSError, match="No space left on device") as exc:
        _transport(OSError("No space left on device")).merge_upsert(_DELTA, "id")
    assert not isinstance(exc.value, ConflictError)
