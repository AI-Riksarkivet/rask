"""The three blob read APIs, exercised — not cited.

`runtime.py` claimed for weeks that placement is "transparent to readers — all four shapes round-trip
identically through `read_blobs`, `take_blobs`, `read_blob_ranges`". Only `read_blobs` had ever been
called. The other two appeared in comments and nowhere else, which is a claim about behaviour nobody
had observed — the same shape of mistake as the `blob_field` regression it sits next to.

So this file calls all three, and pins what each is FOR:

    read_blobs        complete payloads, eagerly, as (id, bytes). The batch/training path.
    take_blobs        BlobFile handles — seek, partial read, streaming. The viewer path: serving a
                      byte range of a 40 MB scan without pulling 40 MB.
    read_blob_ranges  explicit (row, offset, length) triples in ONE call. The path for reading many
                      small slices of many rows — a header probe across a volume, say.

Everything asserted here was measured in-cluster against a real dataset on RustFS first, then reduced
to a local test. Where the two differ, the in-cluster behaviour is the truth and this file is wrong.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest
from ingest.lander import CREATION_FLAGS
from ingest.runtime import BRONZE_SCHEMA
from lance import blob_array, blob_field


#: Big enough to slice meaningfully, small enough to keep the test fast. Placement tier is NOT the
#: subject here — the read APIs behave identically across tiers, which is the point of the claim
#: being tested — so these land inline and that is fine.
PAYLOADS = [b"II*\x00" + b"A" * 500, b"II*\x00" + b"B" * 480, b"II*\x00" + b"C" * 600]


def _dataset(tmp_path: Path, payloads: list[bytes | None], schema: pa.Schema = BRONZE_SCHEMA) -> lance.LanceDataset:
    uri = str(tmp_path / "bronze.lance")
    table = pa.table(
        {
            "id": pa.array(range(len(payloads)), pa.int64()),
            "source_uri": pa.array([f"file:///p{i}.tif" for i in range(len(payloads))]),
            "payload": blob_array(payloads),
        },
        schema=schema,
    )
    lance.write_dataset(table, uri, **CREATION_FLAGS)
    return lance.dataset(uri)


def test_read_blobs_returns_whole_payloads_WITH_their_ids(tmp_path: Path) -> None:
    """The eager path. The id in each tuple is what makes the result mappable back to a row."""
    dataset = _dataset(tmp_path, list(PAYLOADS))

    got = dataset.read_blobs("payload", indices=[0, 1, 2])

    assert [(i, len(b)) for i, b in got] == [(0, 504), (1, 484), (2, 604)]
    assert dict(got)[1] == PAYLOADS[1]


def test_take_blobs_returns_SEEKABLE_handles(tmp_path: Path) -> None:
    """The streaming path, and the reason `take_blobs` exists at all.

    A viewer serving a byte range of a 40 MB scan must not pull 40 MB to answer it. This is the API
    that makes that possible, and until now nothing in this repo had ever opened one.
    """
    dataset = _dataset(tmp_path, list(PAYLOADS))

    handles = dataset.take_blobs("payload", indices=[0])
    handle = handles[0]

    assert handle.size() == len(PAYLOADS[0])
    assert handle.read(4) == b"II*\x00", "a TIFF header should be readable without fetching the page"

    handle.seek(4)
    assert handle.read(4) == b"AAAA"
    assert handle.tell() == 8

    handle.seek(0)
    assert handle.read() == PAYLOADS[0], "a full read after seeking must still yield the whole payload"


def test_read_blob_ranges_reads_SLICES_of_several_rows_in_one_call(tmp_path: Path) -> None:
    """`(row, offset, length)` triples, resolved by `selector`.

    The selector is not optional and not guessable — `indices` (positional), `ids` (the stable row
    id) and `addresses` are three different address spaces, and passing positions while asking for
    ids reads whatever happens to live at those ids.
    """
    dataset = _dataset(tmp_path, list(PAYLOADS))

    requests = [(0, 0, 4), (1, 4, 6), (2, 100, 32)]
    got = dataset.read_blob_ranges("payload", requests, selector="indices")

    assert len(got) == len(requests)
    for (index, offset, length), row in zip(requests, got, strict=True):
        payload = row[-1]
        assert payload == PAYLOADS[index][offset : offset + length]


def test_the_three_APIs_agree_on_the_same_bytes(tmp_path: Path) -> None:
    """The claim `runtime.py` makes, finally asserted: one payload, three ways, identical bytes."""
    dataset = _dataset(tmp_path, list(PAYLOADS))

    eager = dict(dataset.read_blobs("payload", indices=[1]))[1]
    streamed = dataset.take_blobs("payload", indices=[1])[0].read()
    ranged = dataset.read_blob_ranges("payload", [(1, 0, len(PAYLOADS[1]))], selector="indices")[0][-1]

    assert eager == streamed == ranged == PAYLOADS[1]


# ── the null landmine, and why the column is non-nullable ─────────────────────────────


#: The schema as it was — nullable — kept ONLY so the trap can be demonstrated. Deleting this and
#: trusting the prose is how the trap gets rediscovered by whoever relaxes the real schema next.
_NULLABLE_SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("source_uri", pa.string()), blob_field("payload", nullable=True)])


def test_ALL_THREE_apis_silently_drop_a_null_row(tmp_path: Path) -> None:
    """Measured on pylance 9.0.0. Not one API — all three, and each fails differently.

    `read_blobs` is survivable because every tuple carries its id. `read_blob_ranges` answers `[]`
    for a null row with no error. `take_blobs` is the dangerous one: a bare list, no row identity
    anywhere on the handle, so `handles[i]` silently becomes a DIFFERENT row's bytes.
    """
    dataset = _dataset(tmp_path, [PAYLOADS[0], None, PAYLOADS[2]], schema=_NULLABLE_SCHEMA)

    eager = dataset.read_blobs("payload", indices=[0, 1, 2])
    assert [i for i, _ in eager] == [0, 2], "read_blobs dropped the null row but still names the survivors"

    handles = dataset.take_blobs("payload", indices=[0, 1, 2])
    assert len(handles) == 2, "take_blobs dropped the null row"
    assert not any(hasattr(h, "id") or hasattr(h, "row_id") or hasattr(h, "index") for h in handles), (
        "if a handle ever gains row identity, the positional trap below is fixed upstream and this test should be revisited"
    )
    assert handles[1].size() == len(PAYLOADS[2]), "position 1 of the result is ROW 2 — the silent mis-mapping"

    assert dataset.read_blob_ranges("payload", [(1, 0, 2)], selector="indices") == []


def test_bronze_makes_that_state_UNREACHABLE(tmp_path: Path) -> None:
    """The fix is not to document the trap, it is to make the input impossible.

    `BRONZE_SCHEMA` declares `payload` non-nullable, so Lance refuses the write rather than accepting
    rows that every reader will then quietly mis-report. Nothing is given up: the worker parks an
    empty payload (`AcceptAll.check` → "empty payload") instead of writing it, so this plane cannot
    produce the row the old schema permitted.
    """
    assert BRONZE_SCHEMA.field("payload").nullable is False

    with pytest.raises(OSError, match="non-nullable"):
        _dataset(tmp_path, [PAYLOADS[0], None])
