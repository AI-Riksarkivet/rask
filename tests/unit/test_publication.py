"""D-R1/D-R3: a commit is not a publication, and a failed gate must leave consumers on last-good.

The property that matters is not "publish moves a tag" — it is that a reader pinned to `published`
CANNOT see a version the gate rejected, even though that version is committed and any reader doing a
plain `lance.dataset(uri)` sees it immediately. Lance cannot enforce that (there is no quarantined
version); the pointer is what makes it true, so these tests assert from the READER's side wherever
they can.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest
from catalog.services.publication import PUBLISHED_TAG, publish, published_version
from lance_namespace import InvalidInputError, InvalidTableStateError, TableVersionNotFoundError


SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("payload", pa.string())])


def _write(uri: str, ids: list[int | None], *, mode: str = "create") -> int:
    table = pa.table(
        {"id": pa.array(ids, pa.int64()), "payload": pa.array([f"p{i}" for i in range(len(ids))])},
        schema=SCHEMA,
    )
    dataset = lance.write_dataset(table, uri, mode=mode, enable_stable_row_ids=True, data_storage_version="2.2")
    return int(dataset.version)


def _publish(uri: str, version: int):  # noqa: ANN202 — PublicationResult, imported lazily by callers
    return publish(uri, {}, version=version, key_column="id")


def test_nothing_is_published_before_the_first_publish(tmp_path: Path) -> None:
    """An unset tag must read as "nothing published", not blow up.

    pylance RAISES `Ref not found` for an unset tag rather than returning None, so the obvious
    implementation crashes on the FIRST publication of every dataset — the one case that always
    happens.
    """
    uri = str(tmp_path / "t.lance")
    _write(uri, [1, 2, 3])

    assert published_version(uri, {}) is None


def test_a_PASSING_gate_advances_the_pointer_and_names_the_range(tmp_path: Path) -> None:
    """The happy path, and the range D-R3 requires. First publication has no prior version, so the
    delta is "everything up to to_version" and `from_version` is None rather than 0."""
    uri = str(tmp_path / "t.lance")
    version = _write(uri, [1, 2, 3])

    result = _publish(uri, version)

    assert result.published is True
    assert (result.from_version, result.to_version) == (None, version)
    assert published_version(uri, {}) == version


def test_a_FAILING_gate_leaves_CONSUMERS_ON_LAST_GOOD(tmp_path: Path) -> None:
    """The whole point of § D, asserted from the consumer's side.

    Version 1 is good and published. Version 2 is written with a null key — the `not_null` assertion
    fails — and is a real, committed version that any plain reader sees. A consumer reading through
    the pointer must still be on version 1.
    """
    uri = str(tmp_path / "t.lance")
    good = _write(uri, [1, 2, 3])
    _publish(uri, good)

    bad = _write(uri, [4, None, 6], mode="overwrite")
    result = _publish(uri, bad)

    assert result.published is False
    assert result.reason is not None and "not_null" in result.reason
    assert result.to_version == bad, "the rejected version is still NAMED — a blocked batch must be auditable"

    # The pointer did not move...
    assert published_version(uri, {}) == good
    # ...and the bad version is nonetheless committed and visible to anyone NOT using the pointer,
    # which is precisely why the pointer has to exist.
    assert int(lance.dataset(uri).version) == bad

    # The reader's side: pinning to the tag yields the last good rows, not the rejected ones.
    pinned = lance.dataset(uri).checkout_version(PUBLISHED_TAG)
    assert pinned.to_table(columns=["id"]).column("id").to_pylist() == [1, 2, 3]


def test_assertions_come_back_on_BOTH_outcomes(tmp_path: Path) -> None:
    """A rejected batch has to be auditable, not merely refused — the operator needs to know WHICH
    check failed without re-running anything."""
    uri = str(tmp_path / "t.lance")
    version = _write(uri, [1, None])

    result = _publish(uri, version)

    assert result.published is False
    assert [a.assertion for a in result.assertions if not a.success] == ["not_null"]
    assert any(a.success for a in result.assertions), "the passing checks are reported too"


def test_an_empty_version_is_REFUSED(tmp_path: Path) -> None:
    """`row_count_positive`. An empty publication is a silent failure otherwise — a mis-set source
    prefix publishes nothing and reports success."""
    uri = str(tmp_path / "t.lance")
    version = _write(uri, [])

    result = _publish(uri, version)

    assert result.published is False
    assert "row_count_positive" in (result.reason or "")


def test_a_SECOND_publication_carries_the_PREVIOUS_version_as_from(tmp_path: Path) -> None:
    """D-R3's range across successive runs: `from` is the last PUBLISHED version, not the last
    committed one, so a consumer that only ever saw published data gets a contiguous delta."""
    uri = str(tmp_path / "t.lance")
    first = _write(uri, [1, 2])
    _publish(uri, first)

    second = _write(uri, [3, 4], mode="append")
    result = _publish(uri, second)

    assert (result.from_version, result.to_version) == (first, second)
    assert published_version(uri, {}) == second


def test_publishing_BACKWARDS_is_refused(tmp_path: Path) -> None:
    """A late retry of an older run must not silently un-publish newer good data.

    The tag API remains available for a deliberate rollback, where an operator names the version.
    """
    uri = str(tmp_path / "t.lance")
    first = _write(uri, [1, 2])
    second = _write(uri, [3, 4], mode="append")
    _publish(uri, second)

    with pytest.raises(InvalidTableStateError, match="backwards"):
        _publish(uri, first)

    assert published_version(uri, {}) == second


def test_a_version_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    """Fail before reading anything, and name the latest so the caller can see how far off it was."""
    uri = str(tmp_path / "t.lance")
    version = _write(uri, [1])

    with pytest.raises(TableVersionNotFoundError, match="latest"):
        _publish(uri, version + 99)

    with pytest.raises(InvalidInputError):
        _publish(uri, 0)


def test_the_tag_move_mints_NO_new_version(tmp_path: Path) -> None:
    """Publication is metadata-only. If advancing the pointer created a version, every publish would
    itself need publishing — and `cleanup_old_versions` would be reasoning about pointer moves as
    though they were data."""
    uri = str(tmp_path / "t.lance")
    version = _write(uri, [1, 2])

    _publish(uri, version)

    assert int(lance.dataset(uri).version) == version
