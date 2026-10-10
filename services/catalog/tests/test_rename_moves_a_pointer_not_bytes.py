"""A rename edits `__manifest`; it does not copy a dataset — docs/adr/0046-a-rename-moves-a-pointer-not-bytes-2026-09-04.md "A rename moves a POINTER, not bytes".

A rename's cost was the DATASET's size, paid inside a request handler that answered 200. That is the
same class as the compact door before it became a 202, and it is unbounded in a way no pod sizing
fixes: the next table is bigger.

**lance-ns already answers this.** V2 stores a table at
`<hash>_<object_id>` with the mapping in `__manifest` — the hash is there for object-store throughput
and create/delete/recreate conflict prevention, and the spec says the `object_id` suffix "ensures
uniqueness and aids debugging" (`lance_docs/namespace.md`, *Manifest Table Directory*). It is not the
resolution path. So the pointer is the manifest row, and moving it is the whole rename.

MEASURED on the `dir` backend the chart runs (`LANCE_REST_IMPL=dir`, pylance 10.0.0, 2026-09-04):
registering the destination at the SOURCE's location and deregistering the source leaves
`describe_table` resolving to that same location, with rows and version history intact and the
directory keeping its old object_id suffix.

The ONE shape this cannot serve is a V1 ROOT-namespace table (`<name>.lance`, compatibility mode),
where the spec says a rename "transitions to the V2 hash-based path naming" — a relocation. rask's own
`require_parent` guard refuses root tables, so that shape cannot be reached through these doors, and
the refusal below says so rather than quietly copying a dataset inside a request.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest
from lance_namespace import (
    CreateNamespaceRequest,
    DeclareTableRequest,
    DescribeTableRequest,
    InvalidInputError,
    ServiceUnavailableError,
    TableNotFoundError,
    connect,
)

from catalog.services import dataplane
from service_kit.lakehouse.location_claims import ClaimStore


def _namespace(tmp_path: Path):  # noqa: ANN202 - LanceNamespace, no exported protocol
    ns = connect("dir", {"root": str(tmp_path)})
    ns.create_namespace(CreateNamespaceRequest(id=["ns1"]))
    return ns


def _claims(tmp_path: Path) -> ClaimStore:
    """The location claims on the namespace's own root, as the shipped chart lays out the control root."""
    return ClaimStore(control_root=str(tmp_path))


def _written(ns, segments: list[str], *, appends: int = 2) -> str:  # noqa: ANN001
    """A table with version history — the thing a byte copy existed to preserve."""
    location = ns.declare_table(DeclareTableRequest(id=segments)).location
    lance.write_dataset(pa.table({"id": pa.array([1, 2, 3], pa.int64())}), location)
    for i in range(appends):
        lance.write_dataset(pa.table({"id": pa.array([10 + i], pa.int64())}), location, mode="append")
    return location


def test_the_dataset_does_not_move(tmp_path: Path) -> None:
    """The headline. The destination resolves to the SOURCE's own location."""
    ns = _namespace(tmp_path)
    source = _written(ns, ["ns1", "old"])

    new_segments, location = dataplane.rename_table(ns, {}, ["ns1", "old"], "new", None, claims=_claims(tmp_path), delimiter="$")

    assert new_segments == ["ns1", "new"]
    assert location == source, f"the rename relocated the dataset: {location} != {source}"
    assert ns.describe_table(DescribeTableRequest(id=["ns1", "new"])).location == source


def test_renaming_ACROSS_namespaces_still_moves_no_bytes(tmp_path: Path) -> None:
    """The spec allows a rename to change namespace, and under `<hash>_<object_id>` naming the
    namespace is part of the object_id — so this is the case that would most look like it needs a
    relocation, and does not."""
    ns = _namespace(tmp_path)
    ns.create_namespace(CreateNamespaceRequest(id=["ns2"]))
    source = _written(ns, ["ns1", "old"])

    new_segments, location = dataplane.rename_table(ns, {}, ["ns1", "old"], "moved", ["ns2"], claims=_claims(tmp_path), delimiter="$")

    assert new_segments == ["ns2", "moved"]
    assert location == source


def test_a_V1_ROOT_table_is_REFUSED_rather_than_copied(tmp_path: Path) -> None:
    """Compatibility mode stores a root table at `<name>.lance`, where location IS the name, and the
    spec's rule is that renaming it "transitions to the V2 hash-based path naming" — a relocation.

    Refused rather than served by a byte copy: unbounded work in a request handler is the defect this
    change removes, and rask's own `require_parent` guard means no table reachable through these
    doors has this shape anyway. A refusal names the reason; a quiet copy would reintroduce it.
    """
    ns = connect("dir", {"root": str(tmp_path)})
    _written(ns, ["roottable"])

    with pytest.raises(InvalidInputError, match="(?i)root"):
        dataplane.rename_table(ns, {}, ["roottable"], "renamed", None, claims=_claims(tmp_path), delimiter="$")


def test_a_FAILED_registration_puts_the_source_back(tmp_path: Path) -> None:
    """Retiring the source first means a failure after it must restore it, or a legitimate rename that
    trips on its destination leaves the table reachable by NO id — bytes intact and invisible, which is
    the worse half of the trade this ordering makes.

    The compensation is the same `register_table` call the rename itself makes, at the same location.
    """
    ns = _namespace(tmp_path)
    source = _written(ns, ["ns1", "keepme"])

    original = ns.register_table

    def _explode_on_the_destination(request: object) -> object:
        # ONLY the destination fails. A blanket failure would also break the compensation and prove
        # nothing about it — the shape being modelled is a destination that becomes unavailable
        # between the free check and the write (a racing rename taking the name), where restoring the
        # source is exactly what must still work.
        if list(getattr(request, "id", [])) == ["ns1", "newname"]:
            raise RuntimeError("register refused")
        return original(request)

    ns.register_table = _explode_on_the_destination
    try:
        with pytest.raises(RuntimeError, match="register refused"):
            dataplane.rename_table(ns, {}, ["ns1", "keepme"], "newname", None, claims=_claims(tmp_path), delimiter="$")
    finally:
        ns.register_table = original

    assert ns.describe_table(DescribeTableRequest(id=["ns1", "keepme"])).location == source, "a failed rename left the table reachable by no id at all"


def test_a_NESTED_layout_keeps_its_path_instead_of_collapsing_to_the_leaf(tmp_path: Path) -> None:
    """The whole reason the derivation exists. Under a backend that nests, the last segment is not the
    relative path — registering it would point at nothing — and this is the case the dead code was
    written for and never covered."""
    nested = f"file://{tmp_path}/warehouse/tier/ab12_ns1$t"

    assert dataplane._relative_location(nested, root=str(tmp_path)) == "warehouse/tier/ab12_ns1$t"


def test_no_root_falls_back_to_the_leaf_and_says_so(tmp_path: Path) -> None:
    """A caller with no root still gets an answer, because `undrop`'s flat V2 case is served correctly
    by the leaf. What changes is that the fallback is now the stated behaviour of an absent root rather
    than the only reachable branch."""
    assert dataplane._relative_location(f"file://{tmp_path}/ab12_ns1$t", root="") == "ab12_ns1$t"


def test_a_BRANCHED_table_renames_because_nothing_moves(tmp_path: Path) -> None:
    """The refusal this door used to make, and why it is gone.

    A branch is a shallow clone that references its source root by ABSOLUTE path, so the BYTE-COPY
    rename genuinely orphaned every branch: it copied the root, deleted the source, and left the
    branch manifests pointing at bytes that no longer existed — while answering 200. Refusing was
    right for that implementation.

    The pointer move does not copy and does not delete. MEASURED on the `dir` backend: after
    deregistering the source and registering the destination at the SAME location, `branches.list()`
    returns the identical entry (`parent_version`, `branch_identifier`, `manifest_size` all unchanged)
    and the data reads back. There is nothing left to orphan, so the guard refused a safe operation
    for a hazard the implementation no longer has.
    """
    ns = _namespace(tmp_path)
    source = _written(ns, ["ns1", "branched"])
    dataset = lance.dataset(source)
    if not hasattr(dataset, "create_branch"):
        pytest.skip("pylance has no branch API here")
    dataset.create_branch("b1")
    before = lance.dataset(source).branches.list()

    new_segments, location = dataplane.rename_table(ns, {}, ["ns1", "branched"], "renamed", None, claims=_claims(tmp_path), delimiter="$", root=str(tmp_path))

    assert location == source, "the rename relocated a branched dataset"
    assert lance.dataset(location).branches.list() == before, "the branch listing changed under a pointer move"
    assert ns.describe_table(DescribeTableRequest(id=new_segments)).location == source


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(
            "LanceError(IO): Generic S3 error: Error performing list request: Error performing GET http://s/b?list-type=2 in 1ms - "
            'Server returned non-2xx status code: 404 Not Found: <?xml version="1.0" encoding="UTF-8"?><Error><Code>NoSuchBucket'
            "</Code><Message>The specified bucket does not exist</Message></Error>",
            id="the warehouse BUCKET does not exist",
        ),
    ],
)
def test_plan_compaction_does_not_call_a_STORAGE_FAULT_a_missing_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    """The catch was `except ValueError`, and pylance raises `ValueError` for far more than absence.

    A configuration fault, a deleted bucket and a permission failure all reached one handler that
    reported "declared or registered but was never written", so each was rendered as a missing table,
    sending the operator to look for data that was there all along. The bucket case
    is not hypothetical: § Q8-15 measured 79 datasets registered into buckets that do not exist.

    They answer `ServiceUnavailableError` (503) and not a bare re-raise, because a bare one returned a
    500 and a 500 says the CATALOG is broken. Nothing about the request is wrong; the store did not
    answer, which is what the sibling `_already_committed` has always said for the same condition.
    """
    ns = _namespace(tmp_path)
    written = _written(ns, ["ns1", "real"])

    def _fault(*_a: object, **_k: object) -> object:
        raise ValueError(message)

    monkeypatch.setattr(dataplane.lance, "dataset", _fault)
    with pytest.raises(ServiceUnavailableError) as caught:
        dataplane.plan_compaction(written, {}, batch_size=64, num_threads=2, max_source_bytes=256 * 1024 * 1024)

    assert "never written" not in str(caught.value), f"a storage fault was reported as a missing table: {caught.value}"
    assert not isinstance(caught.value, TableNotFoundError), "a storage fault was given the not-found code"
    assert message.split(",")[0][:40] in str(caught.value), "the underlying cause was swallowed rather than carried"
