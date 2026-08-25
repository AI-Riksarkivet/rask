"""Can a blob column cross datasets as a DESCRIPTOR, without re-wrapping the bytes?

`docs/architecture/medallion-data-flow.md` section 9(a). Spec change 1 -- stop materialising blobs in the mover --
depends on the answer, and no Lance source states it.

The finding that shapes this probe: the READ shape and the WRITE shape are NOT symmetric.
A scan hands back ``struct<kind, position, size, blob_id, blob_uri>``; a write demands the
"prepared" form ``struct<kind, data, uri, blob_id, blob_size, position>`` (blob.rs:166). So a
descriptor cannot be round-tripped as-is -- but the mapping between them is pure metadata and
moves no bytes, which is exactly what a mover needs.
"""

import pathlib
import tempfile

import lance
import pyarrow as pa
from lance import Blob, blob_array, blob_field


N = 20
PAYLOADS = [bytes([i]) * 300_000 for i in range(N)]

#: What a write accepts (blob.rs:166). Note `blob_size`/`uri`, NOT `size`/`blob_uri`.
PREPARED = pa.struct(
    [
        pa.field("kind", pa.uint8()),
        pa.field("data", pa.large_binary()),
        pa.field("uri", pa.utf8()),
        pa.field("blob_id", pa.uint32()),
        pa.field("blob_size", pa.uint64()),
        pa.field("position", pa.uint64()),
    ]
)


def _root(name: str) -> str:
    return str(pathlib.Path(tempfile.mkdtemp(prefix=f"carry_{name}_")) / f"{name}.lance")


def _schema() -> pa.Schema:
    return pa.schema([pa.field("id", pa.int64()), blob_field("payload", nullable=True)])


def _as_prepared(scanned: pa.Array) -> pa.Array:
    """Map a SCANNED descriptor onto the prepared write struct. Metadata only -- no bytes."""
    rows = [
        {
            "kind": d["kind"],
            "data": None,
            "uri": d["blob_uri"] or None,
            "blob_id": d["blob_id"],
            "blob_size": d["size"],
            "position": d["position"],
        }
        for d in scanned.to_pylist()
    ]
    return pa.array(rows, type=PREPARED)


def _resolves(ds: lance.LanceDataset) -> tuple[int, str]:
    ok, err = 0, ""
    try:
        for _rid, payload in ds.read_blobs("payload", ids=list(range(N))):
            if payload:
                ok += 1
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:200]
    return ok, err


def case(title: str, source: lance.LanceDataset) -> None:
    print("=" * 74)
    print(title)
    print("=" * 74)
    ok, err = _resolves(source)
    scanned = source.to_table(columns=["payload"]).column("payload").combine_chunks()
    print(f"  source resolves        : {ok}/{N}   {err}")
    print(f"  scanned descriptor[0]  : {scanned[0].as_py()}")
    try:
        dst = lance.write_dataset(
            pa.table({"id": pa.array(range(N)), "payload": _as_prepared(scanned)}, schema=_schema()),
            _root("dst"),
            data_storage_version="2.2",
            enable_stable_row_ids=True,
            allow_external_blob_outside_bases=True,
        )
        ok2, err2 = _resolves(dst)
        print(f"  carried to a 2nd set   : v{dst.version}")
        print(f"  resolves THERE         : {ok2}/{N}   {err2}")
        print(f"  VERDICT                : {'CARRIES' if ok2 == N else 'DOES NOT CARRY'}")
    except Exception as exc:
        print(f"  REFUSED at write       : {type(exc).__name__}: {exc}"[:300])
        print("  VERDICT                : DOES NOT CARRY")
    print()


managed = lance.write_dataset(
    pa.table({"id": pa.array(range(N)), "payload": blob_array(PAYLOADS)}, schema=_schema()),
    _root("managed"),
    data_storage_version="2.2",
    enable_stable_row_ids=True,
)
case("CASE 1 - MANAGED (kind=1/2): sidecar owned by the SOURCE dataset", managed)

container = pathlib.Path(tempfile.mkdtemp(prefix="ext_")) / "corpus.bin"
container.write_bytes(b"".join(PAYLOADS))
uri, off, spans = container.as_uri(), 0, []
for p in PAYLOADS:
    spans.append((off, len(p)))
    off += len(p)
external = lance.write_dataset(
    pa.table(
        {"id": pa.array(range(N)), "payload": blob_array([Blob.from_uri(uri, position=o, size=s) for o, s in spans])},
        schema=_schema(),
    ),
    _root("external"),
    data_storage_version="2.2",
    enable_stable_row_ids=True,
    allow_external_blob_outside_bases=True,
)
case("CASE 2 - EXTERNAL (kind=3): bytes the dataset never owned", external)
