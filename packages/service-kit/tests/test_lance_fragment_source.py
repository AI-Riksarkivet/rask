"""The `lance-append` source: one bronze unit per Lance FRAGMENT.

WHY FRAGMENTS AND NOT ROW OFFSETS. The design first read as a bounded "row range", which the Lance
docs refuse: ``lance_sdk.md`` says offsets "are not stable — a row with an offset of N may have a
different offset in a different version of the table (e.g. if an earlier row is deleted)". The ingest
plane folds a unit key into the bronze row identity so a RE-RUN CONVERGES instead of duplicating, so
an offset-keyed unit would re-land the whole tail of a table after any early delete. ``file_format.md``
gives the stable alternative: ``row_address = (fragment_id << 32) | local_row_offset``, and
``ReserveFragments`` "only changes the max fragment id" — ids are reserved and monotonic, never
renumbered. ``guide.md`` independently treats the fragment as the unit of parallel work.

So the unit key is the fragment id, and the payload is that fragment's rows as an Arrow IPC stream —
opaque bytes in the bronze blob column, which is what lets a source of ANY schema land without the
platform knowing a single column name.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from service_kit.lakehouse.sources import LanceFragmentSource


lance = pytest.importorskip("lance")


@pytest.fixture
def dataset_uri(tmp_path):
    """Three fragments, written one at a time so the ids are distinct and known."""
    uri = str(tmp_path / "src.lance")
    schema = pa.schema([pa.field("id", pa.int64()), pa.field("word", pa.string())])
    lance.write_dataset(pa.table({"id": [1, 2], "word": ["a", "b"]}, schema=schema), uri)
    for rows in ([3, 4], [5, 6]):
        batch = pa.table({"id": rows, "word": [str(value) for value in rows]}, schema=schema)
        lance.write_dataset(batch, uri, mode="append")
    return uri


def test_one_key_per_fragment(dataset_uri: str) -> None:
    keys = list(LanceFragmentSource(dataset_uri).iter_keys())
    assert len(keys) == len(lance.dataset(dataset_uri).get_fragments()) == 3
    assert all(key.startswith(f"{dataset_uri}#fragment=") for key in keys), keys
    assert len(set(keys)) == 3, "fragment keys must be distinct"


def test_keys_are_stable_when_an_earlier_row_is_deleted(dataset_uri: str) -> None:
    """The offset-keyed design would have renumbered every later unit here — the whole reason for fragments."""
    before = list(LanceFragmentSource(dataset_uri).iter_keys())
    dataset = lance.dataset(dataset_uri)
    dataset.delete("id = 1")
    after = list(LanceFragmentSource(dataset_uri).iter_keys())
    assert set(after) <= set(before), f"a delete invented new unit keys: {set(after) - set(before)}"


def test_payload_round_trips_as_arrow_ipc(dataset_uri: str) -> None:
    """Every unit's bytes must reopen as the fragment's own rows — the payload is a real IPC stream."""
    total = 0
    for unit in LanceFragmentSource(dataset_uri).iter_objects():
        table = pa.ipc.open_stream(unit.data).read_all()
        assert table.schema.names == ["id", "word"]
        total += table.num_rows
    assert total == 6


def test_version_token_is_the_dataset_version(dataset_uri: str) -> None:
    """A fragment's CONTENT changes under deletes while its id does not, so the key alone cannot
    identify what was ingested — the dataset version is the token that distinguishes the two reads."""
    source = LanceFragmentSource(dataset_uri)
    first = dict(source.iter_versioned_keys())
    assert all(token is not None for token in first.values())
    lance.dataset(dataset_uri).delete("id = 1")
    second = dict(LanceFragmentSource(dataset_uri).iter_versioned_keys())
    assert set(second) <= set(first), "delete must not invent keys"
    assert set(second.values()) != set(first.values()), "the version token must move when the data does"


def test_missing_dataset_fails_at_construction_not_at_fetch(tmp_path) -> None:
    """Guard 2, restated for this payload shape: a source that cannot be read is refused at ACCEPT
    rather than hanging a worker that already claimed the unit."""
    with pytest.raises((FileNotFoundError, ValueError, OSError)):
        LanceFragmentSource(str(tmp_path / "absent.lance")).probe()
