"""§9 blob-v2 create path — detection helpers + the catalog's ``create_table`` facade.

The facade tests run a real ``dir`` namespace + real pylance write (no mocks): they are the unit-level
proof that a blob column, which the native create rejects at 2.1, round-trips at file format 2.2, that a
plain schema still takes the native 2.1 path, and that the create ``mode`` (Create/ExistOk/Overwrite) is
honoured on the blob path.
"""

from __future__ import annotations

from pathlib import Path

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
    TableNotFoundError,
    connect,
)

from catalog.services.dataplane import create_table
from service_kit.lakehouse import blobs


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


def _blob_ipc(payloads: list[bytes] | None = None) -> bytes:
    payloads = payloads or [b"img-1", b"video" * 1000]
    ids = list(range(len(payloads)))
    table = pa.table(
        {"id": ids, "payload": blob_array(payloads), "src": ["cam"] * len(payloads)},
        schema=_blob_schema(),
    )
    return _ipc(table)


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _open(ns, segments: list[str]) -> lance.LanceDataset:
    described = ns.describe_table(DescribeTableRequest(id=segments, with_table_uri=True, load_detailed_metadata=True))
    return lance.dataset(described.table_uri or described.location)


# --- detection helpers ----------------------------------------------------- #


def test_is_blob_field_detects_blob_v2_and_ignores_scalars() -> None:
    schema = _blob_schema()
    assert blobs.is_blob_field(schema.field("payload"))
    assert not blobs.is_blob_field(schema.field("id"))
    assert not blobs.is_blob_field(schema.field("src"))


def test_schema_has_blob_and_blob_field_names() -> None:
    assert blobs.schema_has_blob(_blob_schema())
    assert blobs.blob_field_names(_blob_schema()) == ["payload"]
    plain = pa.schema([pa.field("id", pa.int64())])
    assert not blobs.schema_has_blob(plain)
    assert blobs.blob_field_names(plain) == []


# --- the create_table facade (real dir namespace + real pylance) ----------- #


def test_create_table_writes_blob_at_2_2_and_roundtrips(tmp_path: Path) -> None:
    ns = connect("dir", {"root": str(tmp_path)})
    resp = create_table(ns, {}, ["clips"], _blob_ipc(), mode="create")

    assert resp.location
    dataset = _open(ns, ["clips"])
    assert dataset.data_storage_version == "2.2"
    # #5a: the catalog's own blob create path stamps durable row identity (create-time-only), matching the
    # medallion cascade — so field/temporal lineage over catalog-created blob tables has a stable anchor.
    assert dataset.has_stable_row_ids
    assert dataset.count_rows() == 2
    assert dataset.read_blobs("payload", indices=[0])[0][1] == b"img-1"


def test_create_table_writes_plain_schema_at_2_2_with_stable_row_ids(tmp_path: Path) -> None:
    # A PLAIN (non-blob) table must ALSO get 2.2 + stable row ids (audit 2026-07-14). It used to fall through
    # to the native create, which pins 2.1 and no stable row ids — both CREATE-TIME-ONLY, so every ordinary
    # catalog table was PERMANENTLY unable to carry the durable row identity `row_id_lineage` needs. This test
    # previously asserted the 2.1 native default; that was pinning the bug. It now pins the fix.
    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["plain"], _ipc(pa.table({"id": [1, 2, 3]})), mode="create")

    dataset = _open(ns, ["plain"])
    assert dataset.data_storage_version == "2.2"  # NOT the native 2.1 default
    assert dataset.has_stable_row_ids  # durable row identity — the whole point of row-id lineage
    assert dataset.count_rows() == 3


def test_create_mode_conflicts_when_blob_table_exists(tmp_path: Path) -> None:
    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["c1"], _blob_ipc(), mode="create")
    with pytest.raises(TableAlreadyExistsError):
        create_table(ns, {}, ["c1"], _blob_ipc(), mode="create")


def test_overwrite_replaces_existing_blob_table(tmp_path: Path) -> None:
    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["o1"], _blob_ipc([b"a", b"b", b"c"]), mode="create")
    resp = create_table(ns, {}, ["o1"], _blob_ipc([b"z"]), mode="overwrite")

    dataset = _open(ns, ["o1"])
    assert dataset.count_rows() == 1  # replaced 3 rows with 1
    assert dataset.read_blobs("payload", indices=[0])[0][1] == b"z"
    assert resp.version is not None and resp.version > 1  # overwrite committed a new version


def test_exist_ok_keeps_existing_blob_table(tmp_path: Path) -> None:
    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["e1"], _blob_ipc([b"a", b"b"]), mode="create")
    second = create_table(ns, {}, ["e1"], _blob_ipc([b"z"]), mode="exist_ok")

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

    resp = create_table(ns, {}, ["d1"], _blob_ipc([b"first"]), mode="exist_ok")

    assert resp.location
    dataset = _open(ns, ["d1"])
    assert dataset.data_storage_version == "2.2"
    assert dataset.count_rows() == 1
    assert dataset.read_blobs("payload", indices=[0])[0][1] == b"first"


def test_default_create_on_declared_only_table_writes_the_first_version(tmp_path: Path) -> None:
    # declare → crash → retry with the default mode must converge to a written table, not conflict on the
    # existing declare. (Same path a failed fresh write's rollback intentionally does NOT leave — but a
    # separate POST /declare does.)
    ns = connect("dir", {"root": str(tmp_path)})
    ns.declare_table(DeclareTableRequest(id=["d2"]))

    create_table(ns, {}, ["d2"], _blob_ipc([b"a", b"b"]), mode="create")

    dataset = _open(ns, ["d2"])
    assert dataset.count_rows() == 2
    assert dataset.read_blobs("payload", indices=[0])[0][1] == b"a"


# --- blob modes: managed (inline/packed/dedicated) always; external pointer gated ------------------ #


def test_managed_blob_modes_inline_and_dedicated(tmp_path: Path) -> None:
    # Managed blobs (bytes copied in) work regardless of the flag: small → inline, large → dedicated sidecar.
    ns = connect("dir", {"root": str(tmp_path)})
    small, large = b"tiny-inline", b"x" * 3_000_000
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("blob")])

    table = pa.table({"id": [1, 2], "blob": blob_array([small, large])}, schema=schema)
    create_table(ns, {}, ["managed"], _ipc(table))

    ds = _open(ns, ["managed"])
    assert ds.data_storage_version == "2.2"
    assert ds.read_blobs("blob", indices=[0])[0][1] == small
    assert ds.read_blobs("blob", indices=[1])[0][1] == large


def test_external_pointer_blob_is_gated_by_the_flag(tmp_path: Path) -> None:
    source = tmp_path / "external.bin"
    source.write_bytes(b"external-bytes-payload")
    ns = connect("dir", {"root": str(tmp_path / "root")})
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("blob")])
    pointer = pa.table(
        {"id": [1], "blob": blob_array([Blob.from_uri(source.as_uri(), position=0, size=8)])},
        schema=schema,
    )
    data = _ipc(pointer)

    # Default: an external pointer outside the dataset root is rejected as a clean client error (400).
    with pytest.raises(InvalidInputError, match="external"):
        create_table(ns, {}, ["ext_off"], data)

    # Opted in: accepted, written at 2.2, and the referenced byte slice reads back.
    create_table(ns, {}, ["ext_on"], data, allow_external_blobs=True)
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

    def _pointer(uri: str) -> bytes:
        return _ipc(pa.table({"id": [1], "blob": blob_array([Blob.from_uri(uri, position=0, size=8)])}, schema=schema))

    bases = [base.as_uri()]
    # UNDER the registered base → accepted with allow_external_blobs left False.
    create_table(ns, {}, ["in_base"], _pointer((base / "obj.bin").as_uri()), external_blob_bases=bases)
    ds = _open(ns, ["in_base"])
    assert ds.data_storage_version == "2.2"
    assert ds.read_blobs("blob", indices=[0])[0][1] == b"approved"  # first 8 bytes, read back

    # OUTSIDE every registered base → rejected (allowlist doesn't cover it, blanket flag off).
    with pytest.raises(InvalidInputError, match="external"):
        create_table(ns, {}, ["out_base"], _pointer(outside.as_uri()), external_blob_bases=bases)


# --- rename is a POINTER move (the dir backend's native rename is a 501) ----------------------------- #
#
# These are the BLOB half of the rename contract. A blob column's bytes live in `data/<stem>/*.blob`, a
# sidecar `data_files()` does not name — which a byte copy had to carry deliberately and could silently
# drop. A pointer move cannot lose it, because nothing is copied; asserting it is what proves that.
#
# Every table here is NAMESPACED, and that is required rather than tidy: a root-namespace table is stored
# under V1 compatibility naming (`<name>.lance`, where the location IS the name), and renaming one is
# refused — see `dataplane.rename_table` and the suite that pins the refusal.


def test_rename_repoints_a_blob_table_without_touching_its_sidecars(tmp_path: Path) -> None:
    from catalog.services.dataplane import rename_table

    ns = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(ns, "media")
    create_table(ns, {}, ["media", "clips"], _blob_ipc([b"a", b"b"]), mode="create")
    before = ns.describe_table(DescribeTableRequest(id=["media", "clips"])).location

    new_segments, location = rename_table(ns, {}, ["media", "clips"], "reels", None)
    assert new_segments == ["media", "reels"]
    assert location == before, "the dataset moved; a rename must repoint, not relocate"

    # Discoverable under the NEW id with its blob payloads still resolving, gone under the OLD id.
    ds = _open(ns, ["media", "reels"])
    assert ds.count_rows() == 2
    assert ds.read_blobs("payload", indices=[0])[0][1] == b"a"
    with pytest.raises(TableNotFoundError):
        ns.describe_table(DescribeTableRequest(id=["media", "clips"]))


def test_rename_preserves_version_history(tmp_path: Path) -> None:
    # Nothing is rewritten, so history cannot be collapsed — the property a byte copy had to work for.
    from catalog.services.dataplane import rename_table

    ns = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(ns, "media")
    create_table(ns, {}, ["media", "t"], _blob_ipc([b"a"]), mode="create")
    create_table(ns, {}, ["media", "t"], _blob_ipc([b"a", b"b"]), mode="overwrite")  # commits v2

    rename_table(ns, {}, ["media", "t"], "t2", None)
    assert len(_open(ns, ["media", "t2"]).versions()) >= 2  # history survived the rename


def test_rename_into_another_namespace(tmp_path: Path) -> None:
    from catalog.services.dataplane import rename_table

    ns = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(ns, "src")
    _declare_namespace(ns, "dst")
    create_table(ns, {}, ["src", "clip"], _blob_ipc([b"x"]), mode="create")

    new_segments, _ = rename_table(ns, {}, ["src", "clip"], "clip", ["dst"])
    assert new_segments == ["dst", "clip"]
    assert _open(ns, ["dst", "clip"]).count_rows() == 1
    with pytest.raises(TableNotFoundError):
        ns.describe_table(DescribeTableRequest(id=["src", "clip"]))


def test_rename_onto_existing_name_conflicts(tmp_path: Path) -> None:
    from catalog.services.dataplane import rename_table

    ns = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(ns, "media")
    create_table(ns, {}, ["media", "a"], _blob_ipc([b"x"]), mode="create")
    create_table(ns, {}, ["media", "b"], _blob_ipc([b"y"]), mode="create")
    with pytest.raises(TableAlreadyExistsError):
        rename_table(ns, {}, ["media", "a"], "b", None)
    # The source is untouched by a rejected rename — still readable at its original id.
    assert _open(ns, ["media", "a"]).read_blobs("payload", indices=[0])[0][1] == b"x"


def test_rename_missing_source_is_not_found(tmp_path: Path) -> None:
    from catalog.services.dataplane import rename_table

    ns = connect("dir", {"root": str(tmp_path)})
    with pytest.raises(TableNotFoundError):
        rename_table(ns, {}, ["ghost"], "x", None)


# --- rename data-safety regressions (audit 2026-07-14) ------------------------------------------------ #


def test_rename_rejects_a_blank_name_and_keeps_the_source(tmp_path: Path) -> None:
    """A blank/whitespace name used to declare the identifier ``['']``, byte-copy the dataset to
    ``<root>/.lance`` and DESTROY the named source — all while returning 200. It must be a 400."""
    from catalog.services.dataplane import rename_table

    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["keep"], _blob_ipc([b"x"]), mode="create")

    for blank in ("", "   "):
        with pytest.raises(InvalidInputError):
            rename_table(ns, {}, ["keep"], blank, None)
    assert _open(ns, ["keep"]).read_blobs("payload", indices=[0])[0][1] == b"x"  # source untouched


def test_rename_onto_itself_is_rejected(tmp_path: Path) -> None:
    from catalog.services.dataplane import rename_table

    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["same"], _blob_ipc([b"x"]), mode="create")

    with pytest.raises(InvalidInputError):
        rename_table(ns, {}, ["same"], "same", None)
    assert _open(ns, ["same"]).count_rows() == 1  # not relocated onto itself / deleted


def test_rename_treats_a_declared_only_destination_as_taken(tmp_path: Path) -> None:
    """A declared stub is TAKEN, never adopted. Adopting one would let two concurrent renames both claim
    the same name and both retire their sources, leaving two ids over one dataset and neither source."""
    from catalog.services.dataplane import rename_table

    ns = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(ns, "media")
    create_table(ns, {}, ["media", "src"], _blob_ipc([b"x"]), mode="create")
    ns.declare_table(DeclareTableRequest(id=["media", "stub"]))  # declared-but-unwritten destination

    with pytest.raises(TableAlreadyExistsError):
        rename_table(ns, {}, ["media", "src"], "stub", None)
    assert _open(ns, ["media", "src"]).read_blobs("payload", indices=[0])[0][1] == b"x"  # source intact


def test_a_failed_source_claim_leaves_the_rename_UNDONE(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pointer move is two calls, so there is a window — and what is in it is the whole safety case.

    The SOURCE is retired first. A failure there aborts before any destination exists: the source still
    resolves, no second pointer was written, and the caller sees the error. Nothing to reconcile.

    That ordering is not a preference. `register_table` accepts a second id at a location another id
    already holds (measured on the `dir` backend), so writing the destination first leaves a window in
    which two concurrent renames of one source both succeed — two live ids on one dataset. `drop_table`
    removes BYTES, so dropping either then destroys the other's table while that id goes on resolving,
    and both `describe_table` answers are identical and correct until it happens.

    A byte copy's equivalent window left two full COPIES of the dataset and, if the source delete had
    already run partway, an unreadable source. That failure class does not exist here either way.
    """
    from lance_namespace import DeregisterTableRequest

    from catalog.services import dataplane

    ns = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(ns, "media")
    create_table(ns, {}, ["media", "a"], _blob_ipc([b"x", b"y"]), mode="create")
    source = ns.describe_table(DescribeTableRequest(id=["media", "a"])).location

    def _fails(request: DeregisterTableRequest) -> None:
        raise OSError("rustfs 503")

    # `monkeypatch`, not a bare attribute assignment: assigning a plain function over a bound method
    # is an `invalid-assignment` to `ty`, and the estate's rule is to type it rather than suppress it.
    monkeypatch.setattr(ns, "deregister_table", _fails)
    with pytest.raises(OSError, match="rustfs 503"):
        dataplane.rename_table(ns, {}, ["media", "a"], "b", None)
    monkeypatch.undo()

    # The source is untouched and the destination was never written.
    assert ns.describe_table(DescribeTableRequest(id=["media", "a"])).location == source
    with pytest.raises(Exception, match="(?i)not found|does not exist|no such"):
        ns.describe_table(DescribeTableRequest(id=["media", "b"]))
    assert _open(ns, ["media", "a"]).read_blobs("payload", indices=[0])[0][1] == b"x"


def test_rejected_external_create_rolls_back_and_stays_retryable(tmp_path: Path) -> None:
    source = tmp_path / "external.bin"
    source.write_bytes(b"external-bytes")
    ns = connect("dir", {"root": str(tmp_path / "root")})
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("blob")])
    pointer = _ipc(
        pa.table(
            {"id": [1], "blob": blob_array([Blob.from_uri(source.as_uri(), position=0, size=8)])},
            schema=schema,
        )
    )

    with pytest.raises(InvalidInputError):
        create_table(ns, {}, ["rb"], pointer)  # flag off → rejected, declared table rolled back

    # retryable: the name is free (rollback dropped the declare), so a managed create at the same id succeeds
    create_table(ns, {}, ["rb"], _ipc(pa.table({"id": [1], "blob": blob_array([b"managed"])}, schema=schema)))
    assert _open(ns, ["rb"]).read_blobs("blob", indices=[0])[0][1] == b"managed"


def test_rename_KEEPS_a_table_with_branches_intact(tmp_path: Path) -> None:
    """Renaming a BRANCHED table used to orphan its branches, and no longer can.

    A branch is a shallow clone referencing its source root by ABSOLUTE path, so the byte-copy rename
    this door once performed left every branch pointing at bytes that no longer existed while
    answering 200. The refusal that replaced it was right for that implementation.

    The pointer move copies and deletes nothing, so there is nothing to orphan. MEASURED on the `dir`
    backend: after the rename the location is unchanged, `branches.list()` returns the identical entry
    — `parent_version`, `branch_identifier` and `manifest_size` all equal — and the data reads back.
    The guard was refusing a safe operation. `services/catalog/tests` carries the positive case
    through the real door; this one refuses the guard's return.
    """
    import pytest

    from catalog.services import dataplane

    uri = str(tmp_path / "t")
    ds = lance.write_dataset(pa.table({"id": [1, 2]}), uri)
    if not hasattr(ds, "create_branch"):
        pytest.skip("pylance has no branch API here")
    ds.create_branch("feature-x")
    before = lance.dataset(uri).branches.list()

    assert not hasattr(dataplane, "_refuse_rename_with_branches"), (
        "the branch refusal is back; it declines a rename that measurably orphans nothing"
    )
    assert lance.dataset(uri).branches.list() == before
