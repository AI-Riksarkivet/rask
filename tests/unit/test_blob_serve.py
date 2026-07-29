"""§9 P1 blob serving path — ``dataplane.read_blob`` + the Range parsing + the authz tier pin.

Like ``test_blob_create.py``, the read tests run a REAL ``dir`` namespace + real pylance (no mocks):
they are the unit-level proof that a credential-less consumer gets exact payload bytes (full and
ranged, streamed in bounded windows) through the catalog, and that every probed pylance failure
shape (zero-length/null empty list, row-address panic on OOB, bare ValueError on a missing version
manifest or a dataset-less declared location) surfaces as a precise 4xx — or valid empty bytes —
instead of a 500.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from catalog.api.fga_deps import _action_relation
from catalog.api.v1.endpoints.data import _parse_range
from catalog.services import dataplane
from catalog.services.dataplane import BlobStream, create_table, read_blob
from lance import blob_array, blob_field
from lance_namespace import (
    DeclareTableRequest,
    InvalidInputError,
    TableColumnNotFoundError,
    TableNotFoundError,
    TableVersionNotFoundError,
    connect,
)


def _blob_schema() -> pa.Schema:
    return pa.schema([pa.field("id", pa.int64()), blob_field("payload"), pa.field("src", pa.string())])


def _blob_ipc(payloads: list[bytes | None]) -> bytes:
    table = pa.table(
        {
            "id": list(range(len(payloads))),
            "payload": blob_array(payloads),
            "src": ["cam"] * len(payloads),
        },
        schema=_blob_schema(),
    )
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _bytes(blob: BlobStream) -> bytes:
    return b"".join(blob.chunks())


@pytest.fixture
def ns(tmp_path: Path):
    namespace = connect("dir", {"root": str(tmp_path)})
    create_table(namespace, {}, ["clips"], _blob_ipc([b"hello-world", b"X" * 100, None, b""]), mode="create")
    return namespace


# --- full + ranged reads ---------------------------------------------------- #


def test_read_blob_full_payload_roundtrips(ns) -> None:
    blob = read_blob(ns, {}, ["clips"], column="payload", row=0)
    assert _bytes(blob) == b"hello-world"
    assert (blob.size, blob.length) == (11, 11)
    assert blob.ranged is False and blob.satisfiable is True


def test_read_blob_bounded_range_slice(ns) -> None:
    blob = read_blob(ns, {}, ["clips"], column="payload", row=0, range_spec=(0, 3))
    assert _bytes(blob) == b"hell"
    assert (blob.start, blob.end, blob.size) == (0, 3, 11)
    assert blob.ranged is True and blob.satisfiable is True


def test_read_blob_open_ended_and_suffix_ranges(ns) -> None:
    open_ended = read_blob(ns, {}, ["clips"], column="payload", row=0, range_spec=(6, None))
    assert _bytes(open_ended) == b"world"
    assert (open_ended.start, open_ended.end) == (6, 10)

    suffix = read_blob(ns, {}, ["clips"], column="payload", row=0, range_spec=(None, 5))
    assert _bytes(suffix) == b"world"
    assert (suffix.start, suffix.end) == (6, 10)

    # A suffix longer than the blob serves the whole payload (RFC 9110 §14.1.2).
    long_suffix = read_blob(ns, {}, ["clips"], column="payload", row=0, range_spec=(None, 500))
    assert _bytes(long_suffix) == b"hello-world"
    assert (long_suffix.start, long_suffix.end) == (0, 10)


def test_read_blob_range_end_clamped_to_size(ns) -> None:
    # read_range itself REJECTS an over-length window (probed: ValueError "exceeds blob size"),
    # so the clamp here is what keeps `bytes=6-9999` a valid 206 instead of a 500.
    blob = read_blob(ns, {}, ["clips"], column="payload", row=0, range_spec=(6, 9999))
    assert _bytes(blob) == b"world"
    assert (blob.start, blob.end, blob.size) == (6, 10, 11)


def test_read_blob_unsatisfiable_range(ns) -> None:
    blob = read_blob(ns, {}, ["clips"], column="payload", row=0, range_spec=(11, None))
    assert blob.satisfiable is False
    assert blob.length == 0
    assert blob.size == 11


def test_read_blob_streams_in_bounded_windows(ns, monkeypatch: pytest.MonkeyPatch) -> None:
    # The chunk loop is what keeps a multi-GB payload out of catalog memory — pin its math by
    # shrinking the window: 11 bytes at window 4 must arrive as 4+4+3, byte-identical.
    monkeypatch.setattr(dataplane, "_BLOB_CHUNK_BYTES", 4)
    blob = read_blob(ns, {}, ["clips"], column="payload", row=0)
    pieces = list(blob.chunks())
    assert pieces == [b"hell", b"o-wo", b"rld"]


def test_read_blob_zero_length_payloads_serve_empty(ns) -> None:
    # At pylance 8.0.0 a NULL blob is stored as a size-0 descriptor (probed: input null_count=1 →
    # stored null_count=0), so null (row 2) and b"" (row 3) are the SAME row state: an empty 200 —
    # never the unguarded files[0] IndexError-500, and never a bogus 400 for valid empty bytes.
    for row in (2, 3):
        blob = read_blob(ns, {}, ["clips"], column="payload", row=row)
        assert blob.satisfiable is True and blob.ranged is False
        assert (blob.size, blob.length) == (0, 0)
        assert _bytes(blob) == b""


def test_read_blob_any_range_on_zero_length_is_unsatisfiable(ns) -> None:
    # RFC 9110: no byte of an empty payload is satisfiable — both plain and suffix ranges 416.
    for spec in ((0, 3), (None, 5)):
        blob = read_blob(ns, {}, ["clips"], column="payload", row=3, range_spec=spec)
        assert blob.satisfiable is False
        assert blob.size == 0


# --- guards: each probed pylance failure shape → a precise 4xx -------------- #


def test_read_blob_unknown_column_is_404(ns) -> None:
    with pytest.raises(TableColumnNotFoundError):
        read_blob(ns, {}, ["clips"], column="nope", row=0)


def test_read_blob_non_blob_column_is_400(ns) -> None:
    with pytest.raises(InvalidInputError, match="not a blob column"):
        read_blob(ns, {}, ["clips"], column="src", row=0)


def test_read_blob_row_out_of_range_is_400(ns) -> None:
    with pytest.raises(InvalidInputError, match="out of range"):
        read_blob(ns, {}, ["clips"], column="payload", row=4)


def test_read_blob_missing_version_is_404(ns) -> None:
    with pytest.raises(TableVersionNotFoundError):
        read_blob(ns, {}, ["clips"], column="payload", row=0, version=999)


def test_read_blob_declared_only_table_is_404_not_500(ns) -> None:
    # A declared-but-never-written table has a location but NO dataset — lance raises a bare
    # ValueError("Dataset at path … was not found"), which must surface as TableNotFound, not 500.
    ns.declare_table(DeclareTableRequest(id=["declared-only"]))
    with pytest.raises(TableNotFoundError, match="no readable dataset"):
        read_blob(ns, {}, ["declared-only"], column="payload", row=0)


def test_read_blob_version_pins_the_payload_and_etag(ns) -> None:
    # Overwrite replaces the data; the pinned read still serves the ORIGINAL bytes, and the etag
    # names the SERVED version so resumable clients can detect the incarnation they started on.
    create_table(ns, {}, ["clips"], _blob_ipc([b"replacement"]), mode="overwrite")
    head = read_blob(ns, {}, ["clips"], column="payload", row=0)
    assert _bytes(head) == b"replacement"
    pinned = read_blob(ns, {}, ["clips"], column="payload", row=0, version=1)
    assert _bytes(pinned) == b"hello-world"
    assert pinned.etag == '"1-payload-0"'
    assert head.etag != pinned.etag


def test_read_blob_if_range_mismatch_downgrades_to_full(ns) -> None:
    # RFC 9110 §13.1.5: a stale If-Range validator (or a date — anything that isn't this read's
    # etag) must serve the FULL payload, never a 206 sliced from different bytes.
    stale = read_blob(ns, {}, ["clips"], column="payload", row=0, range_spec=(6, None), if_range='"999-payload-0"')
    assert stale.ranged is False
    assert _bytes(stale) == b"hello-world"

    current = read_blob(ns, {}, ["clips"], column="payload", row=0, range_spec=(6, None), if_range='"1-payload-0"')
    assert current.ranged is True
    assert _bytes(current) == b"world"


def test_read_blob_rows_are_positional_after_delete(tmp_path: Path) -> None:
    # The documented contract: `row` is the POSITIONAL index at the served version. After a
    # delete, positions shift (probed: take_blobs indices stay consistent with count_rows).
    namespace = connect("dir", {"root": str(tmp_path)})
    create_table(namespace, {}, ["d"], _blob_ipc([b"first", b"second"]), mode="create")

    from catalog.core.namespace import open_dataset

    open_dataset(namespace, {}, ["d"]).delete("id == 0")
    blob = read_blob(namespace, {}, ["d"], column="payload", row=0)
    assert _bytes(blob) == b"second"
    with pytest.raises(InvalidInputError, match="out of range"):
        read_blob(namespace, {}, ["d"], column="payload", row=1)


# --- the pure Range parser --------------------------------------------------- #


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("bytes=0-3", (0, 3)),
        ("bytes=100-", (100, None)),
        ("bytes=-500", (None, 500)),
        (" bytes=0-3 ", (0, 3)),  # surrounding whitespace tolerated
        ("bytes=-", None),  # empty on both sides
        ("bytes=5-2", None),  # last < first → malformed → ignored (full 200)
        ("bytes=0-3,10-20", None),  # multi-range unsupported → ignored
        ("items=0-3", None),  # non-bytes unit → ignored
        ("0-3", None),  # missing unit → ignored
    ],
)
def test_parse_range(header: str | None, expected: tuple[int | None, int | None] | None) -> None:
    assert _parse_range(header) == expected


# --- authz tier pin ----------------------------------------------------------- #


def test_blobs_suffix_is_reader_tier() -> None:
    """CONTRACT: GET /v1/table/{id}/blobs serves DATA, so the router guard must map its suffix to
    reader-tier ``can_read_data`` (same rung as /query) — not the writer-tier fallthrough."""
    assert _action_relation("table", "blobs") == "can_read_data"
