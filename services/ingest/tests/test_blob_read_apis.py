"""The three blob read APIs, exercised — not cited.

`runtime.py` claimed for weeks that placement is "transparent to readers — all four shapes round-trip
identically through `read_blobs`, `take_blobs`, `read_blob_ranges`". Only `read_blobs` had ever been
called. The other two appeared in comments and nowhere else, which is a claim about behaviour nobody
had observed — the same shape of mistake as the `blob_field` regression it sits next to.

So this file calls all three, and pins what each is FOR:

    read_blobs        complete payloads, eagerly, as (ROW INDEX, bytes) — measured, and not the `id`
                      column, which for real bronze rows is a hash. The batch/training path.
    take_blobs        BlobFile handles — seek, partial read, streaming. The viewer path: serving a
                      byte range of a 40 MB scan without pulling 40 MB.
    read_blob_ranges  explicit (row, offset, length) triples in ONE call. The path for reading many
                      small slices of many rows — a header probe across a volume, say.

Everything asserted here was measured in-cluster against a real dataset on RustFS first, then reduced
to a local test. Where the two differ, the in-cluster behaviour is the truth and this file is wrong.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from lance import blob_array, blob_field

from ingest.lander import CREATION_FLAGS
from ingest.runtime import BRONZE_SCHEMA


#: Big enough to slice meaningfully, small enough to keep the test fast. Placement tier is NOT the
#: subject here — the read APIs behave identically across tiers, which is the point of the claim
#: being tested — so these land inline and that is fine.
PAYLOADS = [b"II*\x00" + b"A" * 500, b"II*\x00" + b"B" * 480, b"II*\x00" + b"C" * 600]


def _dataset(tmp_path: Path, payloads: list[bytes | None], schema: pa.Schema = BRONZE_SCHEMA) -> lance.LanceDataset:
    uri = str(tmp_path / "bronze.lance")
    columns: dict[str, pa.Array] = {
        "id": pa.array(range(len(payloads)), pa.int64()),
        "source_uri": pa.array([f"file:///p{i}.tif" for i in range(len(payloads))]),
        "payload": blob_array(payloads),
    }
    # Driven off the schema under test, so one helper serves both the real BRONZE_SCHEMA and the
    # deliberately-nullable variant below (which omits `stage`/`sha256` — it exists only to
    # demonstrate the null trap, and adding columns to it would obscure what it is for).
    if "sha256" in schema.names:
        columns["sha256"] = pa.array([hashlib.sha256(p or b"").hexdigest() for p in payloads], pa.string())
    if "stage" in schema.names:
        columns["stage"] = pa.array(["bronze"] * len(payloads), pa.string())
    if "etag" in schema.names:
        # Same reasoning as partition_key below: irrelevant to the blob APIs, present because
        # the schema declares it.
        columns["etag"] = pa.array([None] * len(payloads), pa.string())
    if "partition_key" in schema.names:
        # Nulls: these fixtures exercise the BLOB read APIs, and the grouping label is irrelevant to
        # them. Present because the schema declares it — a column omitted here is a `KeyError` from
        # inside pyarrow, which reads as a product failure rather than a stale fixture.
        columns["partition_key"] = pa.array([None] * len(payloads), pa.string())
    table = pa.table(columns, schema=schema)
    lance.write_dataset(table, uri, **CREATION_FLAGS)
    return lance.dataset(uri)


def test_read_blobs_returns_whole_payloads_keyed_by_ROW_INDEX(tmp_path: Path) -> None:
    """The eager path, and the key is the ROW INDEX — not the `id` column.

    This said "the id in each tuple is what makes the result mappable back to a row" for as long as
    it existed, and never tested it: `_dataset` numbers `id` as `range(len(payloads))`, so index and
    id are the same integer here and the two readings are indistinguishable. They are not the same
    thing anywhere else — `id` is `identity.unit_id`, a hash of the source key, so in every real
    bronze table it is a large arbitrary int64 no row index will ever equal.

    Believing the old claim is a silent mis-attribution: `dict(read_blobs(...))[some_id]` raises
    KeyError at best, and at worst — for a table whose ids happen to start low — hands back another
    row's bytes. The companion test below pins the distinction with ids that cannot be confused for
    indices.
    """
    dataset = _dataset(tmp_path, list(PAYLOADS))

    got = dataset.read_blobs("payload", indices=[0, 1, 2])

    # Every payload here is non-null, which pylance 10.0.0's `bytes | None` element type cannot know —
    # asserted rather than cast, so the premise is checked instead of assumed.
    assert all(b is not None for _, b in got)
    assert [(i, len(b)) for i, b in got if b is not None] == [(0, 504), (1, 484), (2, 604)]
    assert dict(got)[1] == PAYLOADS[1]


def test_read_blobs_keys_are_INDICES_even_when_the_id_column_says_otherwise(tmp_path: Path) -> None:
    """The measurement that corrected the docstring above, made unmissable.

    Bronze `id`s are hashes of the source key, so this fixture uses ids no index can collide with.
    If `read_blobs` ever starts keying by the `id` column, this fails loudly rather than letting the
    estate map payloads onto the wrong rows.
    """
    uri = str(tmp_path / "bronze.lance")
    table = pa.table(
        {
            "id": pa.array([9_000_001, 9_000_002, 9_000_003], pa.int64()),
            "source_uri": pa.array([f"file:///p{i}.tif" for i in range(3)]),
            "payload": blob_array(list(PAYLOADS)),
            "sha256": pa.array([hashlib.sha256(p).hexdigest() for p in PAYLOADS], pa.string()),
            "etag": pa.array([None] * 3, pa.string()),
            "stage": pa.array(["bronze"] * 3, pa.string()),
            "partition_key": pa.array([None] * 3, pa.string()),
        },
        schema=BRONZE_SCHEMA,
    )
    lance.write_dataset(table, uri, **CREATION_FLAGS)

    got = dict(lance.dataset(uri).read_blobs("payload", indices=[0, 1, 2]))

    assert sorted(got) == [0, 1, 2], "keys are row indices; the id column plays no part"
    assert got[2] == PAYLOADS[2]


def test_take_blobs_returns_SEEKABLE_handles(tmp_path: Path) -> None:
    """The streaming path, and the reason `take_blobs` exists at all.

    A viewer serving a byte range of a 40 MB scan must not pull 40 MB to answer it. This is the API
    that makes that possible, and until now nothing in this repo had ever opened one.
    """
    dataset = _dataset(tmp_path, list(PAYLOADS))

    handles = dataset.take_blobs("payload", indices=[0])
    handle = handles[0]
    # `BlobFile | None` from pylance 10.0.0 — a null payload now occupies its slot rather than being
    # omitted. Row 0's payload is written non-null by the fixture, so this narrows AND states that.
    assert handle is not None

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
    handle = dataset.take_blobs("payload", indices=[1])[0]
    assert handle is not None  # non-null by fixture; narrows pylance 10's `BlobFile | None`
    streamed = handle.read()
    ranged = dataset.read_blob_ranges("payload", [(1, 0, len(PAYLOADS[1]))], selector="indices")[0][-1]

    assert eager == streamed == ranged == PAYLOADS[1]


# ── the null landmine, and why the column is non-nullable ─────────────────────────────


#: The schema as it was — nullable — kept ONLY so the trap can be demonstrated. Deleting this and
#: trusting the prose is how the trap gets rediscovered by whoever relaxes the real schema next.
_NULLABLE_SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("source_uri", pa.string()), blob_field("payload", nullable=True)])


def test_the_null_row_trap_is_FIXED_upstream_and_all_three_apis_keep_position(tmp_path: Path) -> None:
    """Measured on pylance 10.0.0. This asserted the OPPOSITE through 9.0.0, and the flip is the finding.

    Through pylance 9 all three readers silently dropped a null row, each failing differently, and
    `take_blobs` was the dangerous one: a bare list with no row identity on the handle, so `handles[i]`
    silently became a DIFFERENT row's bytes. The old test even said what to do if upstream fixed it —
    "if a handle ever gains row identity, the positional trap below is fixed upstream and this test
    should be revisited". 10.0.0 fixed it a better way than predicted: position is preserved and the
    null occupies its own slot, so identity comes from the index rather than from the handle.

    Kept pointing the other way, because a regression would silently restore the mis-mapping. The
    partner test below — bronze declaring `payload` non-nullable so the state is unreachable — is still
    the real defence, and is unaffected either way.
    """
    dataset = _dataset(tmp_path, [PAYLOADS[0], None, PAYLOADS[2]], schema=_NULLABLE_SCHEMA)

    eager = dataset.read_blobs("payload", indices=[0, 1, 2])
    assert [i for i, _ in eager] == [0, 1, 2], "every selected row is named, the null one included"
    assert dict(eager)[1] is None, "the null row's payload is None, not absent"

    handles = dataset.take_blobs("payload", indices=[0, 1, 2])
    assert len(handles) == 3, "take_blobs no longer drops the null row"
    assert handles[1] is None, "the null occupies its own slot — this is what fixes the positional trap"
    third = handles[2]
    assert third is not None
    assert third.size() == len(PAYLOADS[2]), "position 2 is ROW 2 — no mis-mapping"

    # `read_blob_ranges` changed the SAME way and is the third confirmation of the release's one idea:
    # a null is represented rather than omitted. It answered `[]` through pylance 9; 10.0.0 answers
    # `[(0, 1, None)]` — the request index, the row, and a None payload.
    assert dataset.read_blob_ranges("payload", [(1, 0, 2)], selector="indices") == [(0, 1, None)]


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


# ── the reader the estate actually has ────────────────────────────────────────────────


def test_bronze_satisfies_the_columns_THE_VIEWER_PROJECTS() -> None:
    """Bronze must be openable by the one reader that exists, and this asserts it against that
    reader's own list rather than against a copy of it.

    Found by asking whether anything had ever read these bytes. Nothing had. The media viewer
    projects `_PAGE_COLUMNS = ["id", "source_uri", "stage"]`, `stage` had been dropped from
    `BRONZE_SCHEMA`, and that projection sits OUTSIDE the endpoint's try/except — so every dataset
    this plane wrote answered `GET /api/pages` and `GET /api/page` with a 500:

        Invalid user input: Schema error: No field named stage. Valid fields are id, source_uri.

    Every ingest gate passed throughout, because they all read the dataset directly. The drop was
    recorded in open_ingest.md as cheap and "not fatal" on the reasoning that the movers re-stamp an
    absent `stage` — true for the movers, and irrelevant to a reader that projects it.

    Imported from the viewer rather than restated, so the two cannot drift apart again: a column
    added to the viewer's projection fails HERE, at ingest, which is where it can still be fixed.
    """
    from viewer.api.v1.endpoints.pages import _PAGE_COLUMNS

    missing = [column for column in _PAGE_COLUMNS if column not in BRONZE_SCHEMA.names]

    assert missing == [], f"the viewer projects {missing}, which bronze does not carry — every page read 500s"
