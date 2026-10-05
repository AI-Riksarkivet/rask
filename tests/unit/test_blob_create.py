"""§9 blob-v2 create path — detection helpers + the catalog's ``create_table`` facade.

The facade tests run a real ``dir`` namespace + real pylance write (no mocks): they are the unit-level
proof that a blob column, which the native create rejects at 2.1, round-trips at file format 2.2, that a
plain schema still takes the native 2.1 path, and that the create ``mode`` (Create/ExistOk/Overwrite) is
honoured on the blob path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest
from lance import Blob, blob_array, blob_field
from lance_namespace import (
    CreateNamespaceRequest,
    DeclareTableRequest,
    DescribeTableRequest,
    InvalidInputError,
    TableAlreadyExistsError,
    connect,
)

from catalog.services.dataplane import create_table
from service_kit.lakehouse import blobs
from service_kit.lakehouse.location_claims import ClaimStore


def _declare_namespace(ns: object, name: str) -> None:
    """Create a child namespace before putting a table in it.

    Required since pylance 9.0: the `dir` backend now answers a child-namespace read with
    ``NamespaceNotFoundError: Child namespace reads require an existing __manifest dataset``, where 8.0
    treated any directory as one. Renaming ACROSS namespaces therefore needs both ends declared — the
    destination is read before the move.
    """
    ns.create_namespace(CreateNamespaceRequest(id=[name]))  # ty: ignore[unresolved-attribute]


def _blob_schema() -> pa.Schema:
    return pa.schema([pa.field("id", pa.int64()), blob_field("payload"), pa.field("src", pa.string())])


def _blob_table(payloads: list[bytes] | None = None) -> pa.Table:
    payloads = payloads or [b"img-1", b"video" * 1000]
    ids = list(range(len(payloads)))
    return pa.table(
        {"id": ids, "payload": blob_array(payloads), "src": ["cam"] * len(payloads)},
        schema=_blob_schema(),
    )


def _open(ns, segments: list[str]) -> lance.LanceDataset:
    described = ns.describe_table(DescribeTableRequest(id=segments, with_table_uri=True, load_detailed_metadata=True))
    return lance.dataset(described.table_uri or described.location)


# --- detection helpers ----------------------------------------------------- #


def test_schema_has_blob_and_blob_field_names() -> None:
    assert blobs.schema_has_blob(_blob_schema())
    assert blobs.blob_field_names(_blob_schema()) == ["payload"]
    plain = pa.schema([pa.field("id", pa.int64())])
    assert not blobs.schema_has_blob(plain)
    assert blobs.blob_field_names(plain) == []


# --- the create_table facade (real dir namespace + real pylance) ----------- #


def test_create_table_writes_blob_at_2_2_and_roundtrips(tmp_path: Path) -> None:
    ns = connect("dir", {"root": str(tmp_path)})
    resp = create_table(ns, {}, ["clips"], _blob_table(), mode="create", registry=None)

    assert resp.location
    dataset = _open(ns, ["clips"])
    assert dataset.data_storage_version == "2.2"
    # #5a: the catalog's own blob create path stamps durable row identity (create-time-only), matching the
    # medallion cascade — so field/temporal lineage over catalog-created blob tables has a stable anchor.
    assert dataset.has_stable_row_ids
    assert dataset.count_rows() == 2
    assert dataset.read_blobs("payload", indices=[0])[0][1] == b"img-1"


def test_create_table_writes_plain_schema_at_2_2_with_stable_row_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A PLAIN (non-blob) table gets 2.2 + stable row ids too, both CREATE-TIME-ONLY: without them an
    # ordinary catalog table can never carry the durable row identity `row_id_lineage` needs. The version is
    # asserted on what the door PASSES, because pylance 12's own default is 2.2 and the table alone cannot
    # tell a pinned create from a defaulted one.
    passed: list[Any] = []
    write = lance.write_dataset

    def _spy(*args: Any, **kwargs: Any) -> Any:
        passed.append(kwargs.get("data_storage_version"))
        return write(*args, **kwargs)

    monkeypatch.setattr(lance, "write_dataset", _spy)
    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["plain"], pa.table({"id": [1, 2, 3]}), mode="create", registry=None)

    dataset = _open(ns, ["plain"])
    assert passed == ["2.2"], f"the create door must pin the version, not inherit pylance's default: {passed}"
    assert dataset.data_storage_version == "2.2"
    assert dataset.has_stable_row_ids  # durable row identity — the whole point of row-id lineage
    assert dataset.count_rows() == 3


def test_create_mode_conflicts_when_blob_table_exists(tmp_path: Path) -> None:
    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["c1"], _blob_table(), mode="create", registry=None)
    with pytest.raises(TableAlreadyExistsError):
        create_table(ns, {}, ["c1"], _blob_table(), mode="create", registry=None)


def test_overwrite_replaces_existing_blob_table(tmp_path: Path) -> None:
    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["o1"], _blob_table([b"a", b"b", b"c"]), mode="create", registry=None)
    resp = create_table(ns, {}, ["o1"], _blob_table([b"z"]), mode="overwrite", registry=None)

    dataset = _open(ns, ["o1"])
    assert dataset.count_rows() == 1  # replaced 3 rows with 1
    assert dataset.read_blobs("payload", indices=[0])[0][1] == b"z"
    assert resp.version is not None and resp.version > 1  # overwrite committed a new version


def test_exist_ok_keeps_existing_blob_table(tmp_path: Path) -> None:
    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["e1"], _blob_table([b"a", b"b"]), mode="create", registry=None)
    second = create_table(ns, {}, ["e1"], _blob_table([b"z"]), mode="exist_ok", registry=None)

    # ExistOk keeps the existing table untouched — the 1-row payload is NOT written over the original 2.
    assert second.location
    dataset = _open(ns, ["e1"])
    assert dataset.count_rows() == 2
    assert dataset.read_blobs("payload", indices=[0])[0][1] == b"a"


# --- declared-only tables (POST /declare, and this path's own crash-rollback leaves one) ----------- #


def test_exist_ok_on_declared_only_table_writes_instead_of_500(tmp_path: Path) -> None:
    # A declared-but-unwritten table has NO readable dataset. Opening it to read `.version` (the pre-fix
    # ExistOk path) would raise -> 500. ExistOk must instead land the first data version into that location.
    ns = connect("dir", {"root": str(tmp_path)})
    ns.declare_table(DeclareTableRequest(id=["d1"]))

    resp = create_table(ns, {}, ["d1"], _blob_table([b"first"]), mode="exist_ok", registry=None)

    assert resp.location
    dataset = _open(ns, ["d1"])
    assert dataset.data_storage_version == "2.2"
    assert dataset.count_rows() == 1
    assert dataset.read_blobs("payload", indices=[0])[0][1] == b"first"


# --- blob modes: managed (inline/packed/dedicated) always; external pointer gated ------------------ #


def test_external_pointer_blob_is_gated_by_the_flag(tmp_path: Path) -> None:
    source = tmp_path / "external.bin"
    source.write_bytes(b"external-bytes-payload")
    ns = connect("dir", {"root": str(tmp_path / "root")})
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("blob")])
    pointer = pa.table(
        {"id": [1], "blob": blob_array([Blob.from_uri(source.as_uri(), position=0, size=8)])},
        schema=schema,
    )

    # Default: an external pointer outside the dataset root is rejected as a clean client error (400).
    with pytest.raises(InvalidInputError, match="external"):
        create_table(ns, {}, ["ext_off"], pointer, registry=None)

    # Opted in: accepted, written at 2.2, and the referenced byte slice reads back.
    create_table(ns, {}, ["ext_on"], pointer, allow_external_blobs=True, registry=None)
    ds = _open(ns, ["ext_on"])
    assert ds.data_storage_version == "2.2"
    assert ds.read_blobs("blob", indices=[0])[0][1] == b"external"  # first 8 bytes of the source


def test_external_blob_allowlist_accepts_in_base_rejects_out_of_base(tmp_path: Path) -> None:
    """#92: the SAFER posture. With a registered base allowlist and the blanket flag OFF, an external
    Blob.from_uri UNDER a registered base is accepted, while one OUTSIDE every registered base is rejected —
    a curated media bucket without opening the blanket outside-bases door."""
    base = tmp_path / "approved"
    base.mkdir()
    (base / "obj.bin").write_bytes(b"approved-external-payload")
    outside = tmp_path / "elsewhere.bin"
    outside.write_bytes(b"unapproved-payload")
    ns = connect("dir", {"root": str(tmp_path / "root")})
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("blob")])

    def _pointer(uri: str) -> pa.Table:
        return pa.table({"id": [1], "blob": blob_array([Blob.from_uri(uri, position=0, size=8)])}, schema=schema)

    bases = [base.as_uri()]
    # UNDER the registered base → accepted with allow_external_blobs left False.
    create_table(ns, {}, ["in_base"], _pointer((base / "obj.bin").as_uri()), external_blob_bases=bases, registry=None)
    ds = _open(ns, ["in_base"])
    assert ds.data_storage_version == "2.2"
    assert ds.read_blobs("blob", indices=[0])[0][1] == b"approved"  # first 8 bytes, read back

    # OUTSIDE every registered base → rejected (allowlist doesn't cover it, blanket flag off).
    with pytest.raises(InvalidInputError, match="external"):
        create_table(ns, {}, ["out_base"], _pointer(outside.as_uri()), external_blob_bases=bases, registry=None)


# --- rename is a POINTER move (the dir backend's native rename is a 501) ----------------------------- #
#
# These are the BLOB half of the rename contract. A blob column's bytes live in `data/<stem>/*.blob`, a
# sidecar `data_files()` does not name — which a byte copy had to carry deliberately and could silently
# drop. A pointer move cannot lose it, because nothing is copied; asserting it is what proves that.
#
# Every table here is NAMESPACED, and that is required rather than tidy: a root-namespace table is stored
# under V1 compatibility naming (`<name>.lance`, where the location IS the name), and renaming one is
# refused — see `dataplane.rename_table` and the suite that pins the refusal.


# --- rename data-safety regressions (audit 2026-07-14) ------------------------------------------------ #


def test_rename_rejects_a_blank_name_and_keeps_the_source(tmp_path: Path) -> None:
    """A blank/whitespace name used to declare the identifier ``['']``, byte-copy the dataset to
    ``<root>/.lance`` and DESTROY the named source — all while returning 200. It must be a 400."""
    from catalog.services.dataplane import rename_table

    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["keep"], _blob_table([b"x"]), mode="create", registry=None)

    for blank in ("", "   "):
        with pytest.raises(InvalidInputError):
            rename_table(ns, {}, ["keep"], blank, None, claims=ClaimStore(control_root=str(tmp_path)), delimiter="$")
    assert _open(ns, ["keep"]).read_blobs("payload", indices=[0])[0][1] == b"x"  # source untouched


def test_rename_onto_itself_is_rejected(tmp_path: Path) -> None:
    from catalog.services.dataplane import rename_table

    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["same"], _blob_table([b"x"]), mode="create", registry=None)

    with pytest.raises(InvalidInputError):
        rename_table(ns, {}, ["same"], "same", None, claims=ClaimStore(control_root=str(tmp_path)), delimiter="$")
    assert _open(ns, ["same"]).count_rows() == 1  # not relocated onto itself / deleted


def test_rename_treats_a_declared_only_destination_as_taken(tmp_path: Path) -> None:
    """A declared stub is TAKEN, never adopted. Adopting one would let two concurrent renames both claim
    the same name and both retire their sources, leaving two ids over one dataset and neither source."""
    from catalog.services.dataplane import rename_table

    ns = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(ns, "media")
    create_table(ns, {}, ["media", "src"], _blob_table([b"x"]), mode="create", registry=None)
    ns.declare_table(DeclareTableRequest(id=["media", "stub"]))  # declared-but-unwritten destination

    with pytest.raises(TableAlreadyExistsError):
        rename_table(ns, {}, ["media", "src"], "stub", None, claims=ClaimStore(control_root=str(tmp_path)), delimiter="$")
    assert _open(ns, ["media", "src"]).read_blobs("payload", indices=[0])[0][1] == b"x"  # source intact


def test_a_failed_source_claim_leaves_the_rename_UNDONE(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pointer move is two calls, so there is a window — and what is in it is the whole safety case.

    The location's claim is handed to the destination first ([[LH-204]]) and the SOURCE is retired
    next. A failure there aborts before any destination exists: the source still resolves, no second
    pointer was written, the caller sees the error, and the claim is handed back — left with the
    destination, every later rename of the source would answer code 14 for the claim's lease.

    A byte copy's equivalent window left two full COPIES of the dataset and, if the source delete had
    already run partway, an unreadable source. That failure class does not exist here either way.
    """
    from lance_namespace import DeregisterTableRequest

    from catalog.services import dataplane

    ns = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(ns, "media")
    create_table(ns, {}, ["media", "a"], _blob_table([b"x", b"y"]), mode="create", registry=None)
    source = ns.describe_table(DescribeTableRequest(id=["media", "a"])).location

    def _fails(request: DeregisterTableRequest) -> None:
        raise OSError("rustfs 503")

    # `monkeypatch`, not a bare attribute assignment: assigning a plain function over a bound method
    # is an `invalid-assignment` to `ty`, and the estate's rule is to type it rather than suppress it.
    monkeypatch.setattr(ns, "deregister_table", _fails)
    with pytest.raises(OSError, match="rustfs 503"):
        dataplane.rename_table(ns, {}, ["media", "a"], "b", None, claims=ClaimStore(control_root=str(tmp_path)), delimiter="$")
    monkeypatch.undo()

    # The source is untouched and the destination was never written.
    assert ns.describe_table(DescribeTableRequest(id=["media", "a"])).location == source
    with pytest.raises(Exception, match="(?i)not found|does not exist|no such"):
        ns.describe_table(DescribeTableRequest(id=["media", "b"]))
    assert _open(ns, ["media", "a"]).read_blobs("payload", indices=[0])[0][1] == b"x"
    assert dataplane.rename_table(ns, {}, ["media", "a"], "c", None, claims=ClaimStore(control_root=str(tmp_path)), delimiter="$")[1] == source
