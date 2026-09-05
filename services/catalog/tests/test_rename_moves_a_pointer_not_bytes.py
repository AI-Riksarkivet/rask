"""A rename edits `__manifest`; it does not copy a dataset — docs/DECISIONS.md "A rename moves a POINTER, not bytes".

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
from lance_namespace import CreateNamespaceRequest, DeclareTableRequest, DescribeTableRequest, InvalidInputError, connect

from catalog.services import dataplane


def _namespace(tmp_path: Path):  # noqa: ANN202 - LanceNamespace, no exported protocol
    ns = connect("dir", {"root": str(tmp_path)})
    ns.create_namespace(CreateNamespaceRequest(id=["ns1"]))
    return ns


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

    new_segments, location = dataplane.rename_table(ns, {}, ["ns1", "old"], "new", None)

    assert new_segments == ["ns1", "new"]
    assert location == source, f"the rename relocated the dataset: {location} != {source}"
    assert ns.describe_table(DescribeTableRequest(id=["ns1", "new"])).location == source


def test_the_version_history_survives_because_nothing_was_rewritten(tmp_path: Path) -> None:
    """A read-rewrite would collapse history to v1 and a byte copy had to preserve it deliberately.
    A pointer move cannot lose it, which is one whole class of failure that stops existing."""
    ns = _namespace(tmp_path)
    source = _written(ns, ["ns1", "old"])
    before = lance.dataset(source)
    rows, versions = before.count_rows(), len(before.versions())

    dataplane.rename_table(ns, {}, ["ns1", "old"], "new", None)

    after = lance.dataset(ns.describe_table(DescribeTableRequest(id=["ns1", "new"])).location)
    assert (after.count_rows(), len(after.versions())) == (rows, versions)


def test_the_source_id_stops_resolving(tmp_path: Path) -> None:
    """A rename that left both ids live would be a copy by another name — two governed objects over
    one dataset, and the FGA migration would hand the old id's grants a table that still answers."""
    ns = _namespace(tmp_path)
    _written(ns, ["ns1", "old"])

    dataplane.rename_table(ns, {}, ["ns1", "old"], "new", None)

    with pytest.raises(Exception, match="(?i)not found"):
        ns.describe_table(DescribeTableRequest(id=["ns1", "old"]))


def test_a_taken_destination_is_refused_before_anything_moves(tmp_path: Path) -> None:
    """The pointer move is two calls, so the destination must be proven free FIRST — otherwise the
    register would either fail after the source was already gone or silently adopt a live table."""
    ns = _namespace(tmp_path)
    _written(ns, ["ns1", "old"])
    _written(ns, ["ns1", "taken"])

    with pytest.raises(Exception, match="(?i)already exists"):
        dataplane.rename_table(ns, {}, ["ns1", "old"], "taken", None)

    assert ns.describe_table(DescribeTableRequest(id=["ns1", "old"])).location, "the source was disturbed by a refused rename"


def test_renaming_ACROSS_namespaces_still_moves_no_bytes(tmp_path: Path) -> None:
    """The spec allows a rename to change namespace, and under `<hash>_<object_id>` naming the
    namespace is part of the object_id — so this is the case that would most look like it needs a
    relocation, and does not."""
    ns = _namespace(tmp_path)
    ns.create_namespace(CreateNamespaceRequest(id=["ns2"]))
    source = _written(ns, ["ns1", "old"])

    new_segments, location = dataplane.rename_table(ns, {}, ["ns1", "old"], "moved", ["ns2"])

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
        dataplane.rename_table(ns, {}, ["roottable"], "renamed", None)


def test_TWO_RENAMES_OF_ONE_SOURCE_cannot_both_succeed(tmp_path: Path) -> None:
    """The data-loss shape, and the one a free-destination check cannot catch.

    Both renames check their OWN destination and find it free, so both register a pointer at the
    source's location and both retire the source. The result is two live ids on ONE dataset — and
    `drop_table` removes bytes, so dropping either id destroys the other's table while that id goes on
    resolving. Nothing in the estate reports it: the two `describe_table` answers are identical and
    correct right up until one is dropped.

    Arbitrated by retiring the SOURCE FIRST and letting the backend answer. `deregister_table` is the
    only operation here that can fail for the second caller, because the first has already consumed the
    source pointer — so the race resolves to one winner and one refusal instead of two winners.

    MEASURED on the `dir` backend: `register_table` accepts a second id at a location another id
    already holds, so nothing beneath this function refuses the second rename. Two ids resolving to
    `4eb5ad08_ns1$src` was driven directly before this gate was written.

    Sequential here because the first rename must consume the source for the second to be refused —
    which is exactly the property under test. The ORDER that makes it hold under real concurrency is
    pinned separately below; threading this would make the outcome depend on the scheduler.
    """
    ns = _namespace(tmp_path)
    source = _written(ns, ["ns1", "shared"])

    first, _ = dataplane.rename_table(ns, {}, ["ns1", "shared"], "winner", None)
    with pytest.raises(Exception, match="(?i)not found|does not exist|no such"):
        dataplane.rename_table(ns, {}, ["ns1", "shared"], "loser", None)

    assert first == ["ns1", "winner"]
    assert ns.describe_table(DescribeTableRequest(id=["ns1", "winner"])).location == source
    with pytest.raises(Exception, match="(?i)not found|does not exist|no such"):
        ns.describe_table(DescribeTableRequest(id=["ns1", "loser"]))


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
            dataplane.rename_table(ns, {}, ["ns1", "keepme"], "newname", None)
    finally:
        ns.register_table = original

    assert ns.describe_table(DescribeTableRequest(id=["ns1", "keepme"])).location == source, (
        "a failed rename left the table reachable by no id at all"
    )


def test_the_SOURCE_is_claimed_before_the_destination_exists(tmp_path: Path) -> None:
    """The ordering IS the arbitration, and it is the only thing standing between two racing renames.

    Registering the destination first leaves a window in which both callers have written a pointer and
    neither has retired the source — and `register_table` accepts a second id at an occupied location,
    so nothing refuses them. Retiring the source FIRST makes `deregister_table` the contended
    operation: the second caller finds the pointer already consumed and loses, which is the outcome a
    rename race must have.

    Asserted on the call ORDER rather than on a threaded outcome, because the order is the invariant
    and a race test that passes on timing proves nothing.
    """
    ns = _namespace(tmp_path)
    _written(ns, ["ns1", "ordered"])
    calls: list[str] = []
    real_register, real_deregister = ns.register_table, ns.deregister_table

    def _register(request: object) -> object:
        calls.append("register")
        return real_register(request)

    def _deregister(request: object) -> object:
        calls.append("deregister")
        return real_deregister(request)

    ns.register_table, ns.deregister_table = _register, _deregister
    try:
        dataplane.rename_table(ns, {}, ["ns1", "ordered"], "renamed", None)
    finally:
        ns.register_table, ns.deregister_table = real_register, real_deregister

    assert calls[:2] == ["deregister", "register"], f"the destination was written before the source was claimed: {calls}"
