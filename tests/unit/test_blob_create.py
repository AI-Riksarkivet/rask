"""§9 blob-v2 create path — detection helpers + the catalog's ``create_table`` facade.

The facade tests run a real ``dir`` namespace + real pylance write (no mocks): they are the unit-level
proof that a blob column, which the native create rejects at 2.1, round-trips at file format 2.2, that a
plain schema still takes the native 2.1 path, and that the create ``mode`` (Create/ExistOk/Overwrite) is
honoured on the blob path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pytest
from lance import blob_array, blob_field
from lance_namespace import (
    CreateNamespaceRequest,
    DeclareTableRequest,
    DescribeTableRequest,
    DropTableRequest,
    DropTableResponse,
    InvalidInputError,
    LanceNamespace,
    TableAlreadyExistsError,
    connect,
)

from catalog.services import table_claims
from catalog.services.dataplane import create_table
from lakehouse import blobs
from lakehouse.location_claims import ClaimStore
from lakehouse.storage_options import StorageOptions


def _declare_namespace(ns: LanceNamespace, name: str) -> None:
    """Create a child namespace before putting a table in it.

    Required since pylance 9.0: the `dir` backend now answers a child-namespace read with
    ``NamespaceNotFoundError: Child namespace reads require an existing __manifest dataset``, where 8.0
    treated any directory as one. Renaming ACROSS namespaces therefore needs both ends declared — the
    destination is read before the move.
    """
    ns.create_namespace(CreateNamespaceRequest(id=[name]))


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
    _declare_namespace(ns, "db")
    resp = create_table(ns, StorageOptions(), ["db", "clips"], _blob_table(), root=str(tmp_path), mode="create", registry=None)

    assert resp.location
    dataset = _open(ns, ["db", "clips"])
    assert dataset.data_storage_version == "2.2"
    # #5a: the catalog's own blob create path stamps durable row identity (create-time-only), as every
    # catalog create does — so field/temporal lineage over catalog-created blob tables has a stable anchor.
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
    _declare_namespace(ns, "db")
    create_table(ns, StorageOptions(), ["db", "plain"], pa.table({"id": [1, 2, 3]}), root=str(tmp_path), mode="create", registry=None)

    dataset = _open(ns, ["db", "plain"])
    assert passed == ["2.2"], f"the create door must pin the version, not inherit pylance's default: {passed}"
    assert dataset.data_storage_version == "2.2"
    assert dataset.has_stable_row_ids  # durable row identity — the whole point of row-id lineage
    assert dataset.count_rows() == 3


def test_overwrite_replaces_existing_blob_table(tmp_path: Path) -> None:
    ns = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(ns, "db")
    create_table(ns, StorageOptions(), ["db", "o1"], _blob_table([b"a", b"b", b"c"]), root=str(tmp_path), mode="create", registry=None)
    resp = create_table(ns, StorageOptions(), ["db", "o1"], _blob_table([b"z"]), root=str(tmp_path), mode="overwrite", registry=None)

    dataset = _open(ns, ["db", "o1"])
    assert dataset.count_rows() == 1  # replaced 3 rows with 1
    assert dataset.read_blobs("payload", indices=[0])[0][1] == b"z"
    assert resp.version is not None and resp.version > 1  # overwrite committed a new version


def test_exist_ok_keeps_existing_blob_table(tmp_path: Path) -> None:
    ns = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(ns, "db")
    create_table(ns, StorageOptions(), ["db", "e1"], _blob_table([b"a", b"b"]), root=str(tmp_path), mode="create", registry=None)
    second = create_table(ns, StorageOptions(), ["db", "e1"], _blob_table([b"z"]), root=str(tmp_path), mode="exist_ok", registry=None)

    # ExistOk keeps the existing table untouched — the 1-row payload is NOT written over the original 2.
    assert second.location
    dataset = _open(ns, ["db", "e1"])
    assert dataset.count_rows() == 2
    assert dataset.read_blobs("payload", indices=[0])[0][1] == b"a"


# --- declared-only tables (POST /declare, and this path's own crash-rollback leaves one) ----------- #


def test_exist_ok_on_declared_only_table_writes_instead_of_500(tmp_path: Path) -> None:
    # A declared-but-unwritten table has NO readable dataset. Opening it to read `.version` (the pre-fix
    # ExistOk path) would raise -> 500. ExistOk must instead land the first data version into that location.
    ns = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(ns, "db")
    ns.declare_table(DeclareTableRequest(id=["db", "d1"]))

    resp = create_table(ns, StorageOptions(), ["db", "d1"], _blob_table([b"first"]), root=str(tmp_path), mode="exist_ok", registry=None)

    assert resp.location
    dataset = _open(ns, ["db", "d1"])
    assert dataset.data_storage_version == "2.2"
    assert dataset.count_rows() == 1
    assert dataset.read_blobs("payload", indices=[0])[0][1] == b"first"


class _DropRefused:
    """The real namespace, whose drop the store refuses."""

    def __init__(self, real: LanceNamespace) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 — every other operation is the real backend's
        return getattr(self._real, name)

    def drop_table(self, request: DropTableRequest) -> DropTableResponse:
        raise OSError("the store refused the delete")


def test_a_create_whose_write_and_rollback_both_fail_answers_the_write_and_logs_the_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The write's error is what the caller is told; a rollback that fails too leaves the name declared with no dataset
    behind it, which is logged with its cause rather than swallowed ([[LH-572]])."""

    def _refused_write(*_args: Any, **_kwargs: Any) -> Any:  # noqa: ANN401 — `lance.write_dataset`'s own signature
        raise RuntimeError("the store refused the write")

    monkeypatch.setattr(lance, "write_dataset", _refused_write)
    real = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(real, "db")

    with caplog.at_level("ERROR", logger="catalog.services.dataplane"), pytest.raises(RuntimeError, match="refused the write"):
        create_table(
            cast("LanceNamespace", _DropRefused(real)),
            StorageOptions(),
            ["db", "plain"],
            pa.table({"id": [1]}),
            root=str(tmp_path),
            mode="create",
            registry=None,
        )

    logged = [(record.getMessage(), getattr(record, "error", None)) for record in caplog.records if record.name == "catalog.services.dataplane"]
    assert logged == [("create_rollback_drop_failed", "the store refused the delete")]


# --- blob modes: managed (inline/packed/dedicated) always; external pointer gated ------------------ #


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
    _declare_namespace(ns, "db")
    create_table(ns, StorageOptions(), ["db", "keep"], _blob_table([b"x"]), root=str(tmp_path), mode="create", registry=None)

    for blank in ("", "   "):
        with pytest.raises(InvalidInputError):
            rename_table(
                ns,
                table_claims.resolve(ns, ClaimStore(control_root=str(tmp_path)), ["db", "keep"]),
                blank,
                None,
                so=StorageOptions(),
                claims=ClaimStore(control_root=str(tmp_path)),
                root=str(tmp_path),
            )
    assert _open(ns, ["db", "keep"]).read_blobs("payload", indices=[0])[0][1] == b"x"  # source untouched


def test_rename_onto_itself_is_rejected(tmp_path: Path) -> None:
    from catalog.services.dataplane import rename_table

    ns = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(ns, "db")
    create_table(ns, StorageOptions(), ["db", "same"], _blob_table([b"x"]), root=str(tmp_path), mode="create", registry=None)

    with pytest.raises(InvalidInputError):
        rename_table(
            ns,
            table_claims.resolve(ns, ClaimStore(control_root=str(tmp_path)), ["db", "same"]),
            "same",
            None,
            so=StorageOptions(),
            claims=ClaimStore(control_root=str(tmp_path)),
            root=str(tmp_path),
        )
    assert _open(ns, ["db", "same"]).count_rows() == 1  # not relocated onto itself / deleted


def test_rename_treats_a_declared_only_destination_as_taken(tmp_path: Path) -> None:
    """A declared stub is TAKEN, never adopted. Adopting one would let two concurrent renames both claim
    the same name and both retire their sources, leaving two ids over one dataset and neither source."""
    from catalog.services.dataplane import rename_table

    ns = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(ns, "media")
    create_table(ns, StorageOptions(), ["media", "src"], _blob_table([b"x"]), root=str(tmp_path), mode="create", registry=None)
    ns.declare_table(DeclareTableRequest(id=["media", "stub"]))  # declared-but-unwritten destination

    with pytest.raises(TableAlreadyExistsError):
        rename_table(
            ns,
            table_claims.resolve(ns, ClaimStore(control_root=str(tmp_path)), ["media", "src"]),
            "stub",
            None,
            so=StorageOptions(),
            claims=ClaimStore(control_root=str(tmp_path)),
            root=str(tmp_path),
        )
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

    from catalog.services import dataplane, table_claims

    ns = connect("dir", {"root": str(tmp_path)})
    _declare_namespace(ns, "media")
    create_table(ns, StorageOptions(), ["media", "a"], _blob_table([b"x", b"y"]), root=str(tmp_path), mode="create", registry=None)
    source = ns.describe_table(DescribeTableRequest(id=["media", "a"])).location

    def _fails(request: DeregisterTableRequest) -> None:
        raise OSError("rustfs 503")

    # `monkeypatch`, not a bare attribute assignment: assigning a plain function over a bound method
    # is an `invalid-assignment` to `ty`, and the estate's rule is to type it rather than suppress it.
    monkeypatch.setattr(ns, "deregister_table", _fails)
    with pytest.raises(OSError, match="rustfs 503"):
        dataplane.rename_table(
            ns,
            table_claims.resolve(ns, ClaimStore(control_root=str(tmp_path)), ["media", "a"]),
            "b",
            None,
            so=StorageOptions(),
            claims=ClaimStore(control_root=str(tmp_path)),
            root=str(tmp_path),
        )
    monkeypatch.undo()

    # The source is untouched and the destination was never written.
    assert ns.describe_table(DescribeTableRequest(id=["media", "a"])).location == source
    with pytest.raises(Exception, match="(?i)not found|does not exist|no such"):
        ns.describe_table(DescribeTableRequest(id=["media", "b"]))
    assert _open(ns, ["media", "a"]).read_blobs("payload", indices=[0])[0][1] == b"x"
    assert (
        dataplane.rename_table(
            ns,
            table_claims.resolve(ns, ClaimStore(control_root=str(tmp_path)), ["media", "a"]),
            "c",
            None,
            so=StorageOptions(),
            claims=ClaimStore(control_root=str(tmp_path)),
            root=str(tmp_path),
        )[1]
        == source
    )
