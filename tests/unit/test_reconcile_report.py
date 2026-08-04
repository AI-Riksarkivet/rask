"""The cross-store reconciler (`open_hierarchy_lifecycle.md` Decision 4) — REPORT ONLY (unit).

The estate is built with the CATALOG's own registry writers against a local filesystem control root
(``file://{tmp_path}``, exactly as ``tests/unit/test_warehouses.py`` does), so these tests prove the
reconciler reads what the catalog actually writes rather than a copy of the layout that could drift.
OpenFGA and S3 are faked at the seam the reconciler uses (``fga.read_tuples`` / ``list_buckets``).

Two invariants carry the module and are asserted MECHANICALLY, not by comment:
:func:`test_the_module_contains_no_mutating_call` walks the AST of ``reconcile.py`` for any call that
could write, and :func:`test_a_full_run_leaves_every_store_byte_identical` snapshots the control root
before and after a run and drives it with stores whose write paths raise.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import io
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pytest
from catalog.services import projects as proj_svc
from catalog.services import warehouses as wh_svc
from lance_namespace import CreateNamespaceRequest, CreateTableRequest, ServiceUnavailableError, connect
from maintenance.core.config import CompactionSettings
from maintenance.services import reconcile as mod
from openfga_sdk.client.models import ClientTuple
from pydantic import SecretStr

from service_kit.governed import fga as fga_module


_PRIMARY_BUCKET = "lance-catalog"

#: The categories whose answer comes (wholly or partly) from OpenFGA — so an FGA outage must degrade
#: EXACTLY these and nothing else. Kept as one named set rather than repeated literals: a new
#: authz-derived category that forgets to degrade is the failure this pins, and a hand-copied literal
#: in two tests is how that gets "fixed" by editing the expectation instead of the code.
_FGA_DERIVED_CATEGORIES = {"ghost_projects", "ghost_warehouses", "unreferenced_projects", "orphaned_annotation_tasks"}


# --------------------------------------------------------------------------- #
# fixtures: a real registry on a file:// control root + faked FGA/S3 stores
# --------------------------------------------------------------------------- #


class _FakeFga:
    """A stand-in for the OpenFGA store's READ half, keyed the way the reconciler scans it.

    ``tuples`` maps a full object (``"project:acme"``) to its (user, relation) pairs. ``read_tuples``
    honours the bare-type filter (``obj="project:"``) the scan sends, and can be made to fail or to
    page forever — the two behaviours the report must survive without lying.
    """

    def __init__(self, tuples: dict[str, list[tuple[str, str]]] | None = None, *, error: Exception | None = None, endless: bool = False) -> None:
        self.tuples = tuples or {}
        self.error = error
        self.endless = endless
        self.writes: list[Any] = []

    async def read_tuples(self, _client: object, *, obj: str | None = None, **_kw: object) -> tuple[list[ClientTuple], str | None]:
        if self.error is not None:
            raise self.error
        prefix = obj or ""
        page = [
            ClientTuple(user=user, relation=relation, object=name)
            for name, pairs in sorted(self.tuples.items())
            for user, relation in pairs
            if name.startswith(prefix)
        ]
        return page, ("more" if self.endless else None)

    async def write_tuples(self, *_a: object, **_kw: object) -> None:
        raise AssertionError("the reconciler wrote FGA tuples — it is report-only")

    async def delete_tuples(self, *_a: object, **_kw: object) -> None:
        raise AssertionError("the reconciler deleted FGA tuples — it is report-only")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fga_module, "read_tuples", self.read_tuples)
        monkeypatch.setattr(fga_module, "write_tuples", self.write_tuples)
        monkeypatch.setattr(fga_module, "delete_tuples", self.delete_tuples)


class _FakeS3:
    """The bucket-listing half of an S3 client. Every write verb raises: a report that touches object
    storage is a defect, and this makes it a FAILURE rather than a silent success."""

    def __init__(self, buckets: list[str], *, error: Exception | None = None, truncated: bool = False) -> None:
        self.buckets = buckets
        self.error = error
        self.truncated = truncated

    def list_buckets(self) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        body: dict[str, Any] = {"Buckets": [{"Name": name} for name in self.buckets]}
        if self.truncated:
            body["ContinuationToken"] = "next-page"
        return body

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the reconciler called S3 {name}() — it is report-only")


def _settings() -> CompactionSettings:
    return CompactionSettings(s3_bucket=_PRIMARY_BUCKET, s3_secret_access_key=SecretStr("unit"))


class _Estate:
    """A consistent estate: one project, one warehouse, one bound namespace, and the tuples for both."""

    def __init__(self, tmp_path: Path) -> None:
        self.control_root = f"file://{tmp_path / 'control'}"
        self.namespace_root = f"file://{tmp_path / 'data'}"
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        proj_svc.put_project(self.control_root, {}, {"id": "acme", "created_at": "t", "created_by": "alice", "protected": "false"})
        wh_svc.put_warehouse(
            self.control_root,
            {},
            {"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme", "created_at": "t"},
        )
        wh_svc.bind_namespace(self.control_root, {}, "team_ns", "wh-a", "s3://bkt-a")
        self.namespace_dir(tmp_path, "team_ns")
        self.fga = _FakeFga(
            {
                "project:acme": [("user:alice", "admin")],
                "warehouse:wh-a": [("user:alice", "owner"), ("project:acme", "project")],
                # The estate root: real tuples, no registry record, BY DESIGN.
                mod.DEFAULT_FGA_ROOT_OBJECT: [("user:alice", "owner")],
            }
        )
        self.s3 = _FakeS3([_PRIMARY_BUCKET, "bkt-a"])

    @staticmethod
    def namespace_dir(tmp_path: Path, name: str, *, dataset: bool = False) -> None:
        """Create a top-level namespace on the shared default root through the CATALOG'S OWN BACKEND —
        ``lance_namespace.connect("dir", …)``, the impl the chart runs (``LANCE_REST_IMPL=dir``).

        Not ``mkdir``. A namespace is NOT a directory on this impl: ``create_namespace`` writes a row in
        ``__manifest`` and touches nothing else, while a table lands flat at the root as
        ``<uuid>_<ns>$<table>/``. A fixture that hand-builds directories would let a reconciler that
        scans prefixes pass here and find nothing at all on a real estate — the exact failure this file
        is supposed to gate. With ``dataset`` a real top-level TABLE is created instead, which is the
        thing a prefix scan mistakes for a namespace.
        """
        ns = connect("dir", {"root": str(tmp_path / "data")})
        if not dataset:
            ns.create_namespace(CreateNamespaceRequest(id=[name]))
            return
        sink = io.BytesIO()
        arrow = pa.table({"a": pa.array([1])})
        with pa.ipc.new_stream(sink, arrow.schema) as writer:
            writer.write_table(arrow)
        ns.create_table(CreateTableRequest(id=[name]), sink.getvalue())

    def run(self, monkeypatch: pytest.MonkeyPatch, *, warehouses_enabled: bool = True) -> mod.ReconcileReport:
        self.fga.install(monkeypatch)
        return asyncio.run(
            mod.reconcile(
                _settings(),
                cast(Any, object()),
                warehouses_enabled=warehouses_enabled,
                control_root=self.control_root,
                namespace_root=self.namespace_root,
                bucket_client=self.s3,
            )
        )


# --------------------------------------------------------------------------- #
# the baseline: a consistent estate has nothing to say
# --------------------------------------------------------------------------- #


def test_a_fully_consistent_estate_reports_no_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every store agrees → every category is CHECKED and empty. The counts carry all six names, so
    "clean" is provable rather than inferred from a report that simply said nothing."""
    report = _Estate(tmp_path).run(monkeypatch)
    assert report.total == 0
    assert set(report.counts) == set(mod.CATEGORIES)
    assert report.unavailable == []
    assert report.skipped == []
    assert report.incomplete == []


# --------------------------------------------------------------------------- #
# 1 + 2 — ghost tenants: authz for something the registry never heard of
# --------------------------------------------------------------------------- #


def test_ghost_projects_fires_on_an_fga_project_with_no_registry_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Today's seeded ghosts: tuples exist, the tenant does not. The finding names the object AND the
    size of its grant surface, because "there is a ghost" is not actionable without knowing how big."""
    estate = _Estate(tmp_path)
    estate.fga.tuples["project:phantom"] = [("user:mallory", "admin"), ("user:bob", "admin")]
    report = estate.run(monkeypatch)
    assert [(g.fga_object, g.tuples) for g in report.ghost_projects] == [("project:phantom", 2)]
    assert report.counts["ghost_projects"] == 1
    assert report.total == 1


def test_ghost_warehouses_fires_but_never_on_the_estate_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The root object (`Settings.fga_root_object`) holds real tuples and has no registry record BY
    DESIGN — reporting it every run would train the reader to skip the category. A real ghost still fires."""
    estate = _Estate(tmp_path)
    report = estate.run(monkeypatch)
    assert report.ghost_warehouses == []  # the root object alone is NOT drift

    estate.fga.tuples["warehouse:wh-vanished"] = [("user:alice", "owner")]
    report = estate.run(monkeypatch)
    assert [g.fga_object for g in report.ghost_warehouses] == ["warehouse:wh-vanished"]


# --------------------------------------------------------------------------- #
# 3 — unreferenced projects: a tenant nobody can administer, hence nobody can delete
# --------------------------------------------------------------------------- #


def test_unreferenced_projects_fires_on_a_record_with_no_tuples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mirror image of a ghost, and just as undeletable: the record exists, so every door 404s no
    one — but no tuple grants anyone `can_administer`, so the delete door can never be passed either."""
    estate = _Estate(tmp_path)
    proj_svc.put_project(estate.control_root, {}, {"id": "orphaned", "created_at": "t", "created_by": "gone", "protected": "false"})
    report = estate.run(monkeypatch)
    assert [p.id for p in report.unreferenced_projects] == ["orphaned"]
    assert [p.id for p in mod._unreferenced_projects({"acme"}, {"acme": 1})] == []  # ...and silent when tuples exist


# --------------------------------------------------------------------------- #
# 4 — unbound namespaces, gated on the warehouse feature
# --------------------------------------------------------------------------- #


def test_unbound_namespaces_fires_on_a_pre_rule_namespace_with_no_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A top-level namespace on the SHARED default root that no binding claims — it silently resolves
    to the shared bucket, which is exactly the legacy the warehouse rule replaced. The BOUND sibling
    beside it stays silent, so the binding is what suppresses the finding, not the scan missing it."""
    estate = _Estate(tmp_path)
    _Estate.namespace_dir(tmp_path, "legacy_ns")
    report = estate.run(monkeypatch)
    assert [n.namespace for n in report.unbound_namespaces] == ["legacy_ns"]
    assert report.unbound_namespaces[0].root == estate.namespace_root


def test_a_top_level_table_is_not_mistaken_for_an_unbound_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A top-level TABLE is the only thing the catalog DOES lay out as a root directory — and it is not
    drift. The manifest's `object_type` tells them apart; a scan keying on the directory would report
    every root-level table as an unbound namespace forever."""
    estate = _Estate(tmp_path)
    _Estate.namespace_dir(tmp_path, "orders", dataset=True)
    report = estate.run(monkeypatch)
    assert report.unbound_namespaces == []


def test_a_namespace_is_not_a_directory_so_the_scan_must_read_the_manifest(tmp_path: Path) -> None:
    """The invariant the whole category rests on, pinned against the REAL backend.

    ``create_namespace`` on the ``dir`` impl (``LANCE_REST_IMPL=dir``, chart/templates/services.yaml)
    writes a ``__manifest`` row and creates NO directory — a TABLE is the only thing that materialises
    one. A reconciler that listed prefixes would therefore find zero namespaces on every real estate
    and report `unbound_namespaces: 0` — "checked and clean" — forever, while every table it did find
    was a false positive. This test fails the moment someone reintroduces a prefix scan.
    """
    _Estate.namespace_dir(tmp_path, "legacy_ns")
    entries = sorted(p.name for p in (tmp_path / "data").iterdir())
    assert entries == ["__manifest"], f"a namespace materialised something on disk: {entries}"

    _Estate.namespace_dir(tmp_path, "orders", dataset=True)
    table_dirs = sorted(p.name for p in (tmp_path / "data").iterdir() if p.name != "__manifest")
    assert table_dirs == ["orders.lance"], f"a table is the only root directory the catalog makes: {table_dirs}"

    found = mod._top_level_namespaces(f"file://{tmp_path / 'data'}", {}, "$")
    assert found == ["legacy_ns"], "the manifest read must see the namespace and NOT the table directory"


def test_unbound_namespaces_is_skipped_not_zero_when_warehouses_are_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the feature off an unbound namespace is CORRECT. The category is recorded as SKIPPED and
    kept out of `counts` — a 0 there would read as "checked and clean", which it is not."""
    estate = _Estate(tmp_path)
    _Estate.namespace_dir(tmp_path, "legacy_ns")
    report = estate.run(monkeypatch, warehouses_enabled=False)
    assert report.unbound_namespaces == []
    assert [s.category for s in report.skipped] == ["unbound_namespaces"]
    assert "unbound_namespaces" not in report.counts
    assert report.total == 0


# --------------------------------------------------------------------------- #
# 5 — orphan buckets: bytes that outlived the record naming them
# --------------------------------------------------------------------------- #


def test_orphan_buckets_fires_on_a_bucket_no_warehouse_record_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A warehouse delete removes the record; the bucket survives unless someone asked to purge it.
    The claimed bucket and the platform's own primary bucket stay silent."""
    estate = _Estate(tmp_path)
    estate.s3 = _FakeS3([_PRIMARY_BUCKET, "bkt-a", "bkt-deleted-without-purge"])
    report = estate.run(monkeypatch)
    assert [b.bucket for b in report.orphan_buckets] == ["bkt-deleted-without-purge"]


# --------------------------------------------------------------------------- #
# 6 — dangling bindings: a namespace routed at a warehouse that is gone
# --------------------------------------------------------------------------- #


def test_dangling_bindings_fires_when_the_bound_warehouse_has_no_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The routing resolver fails CLOSED on this (403), so the namespace is unreachable rather than
    misrouted — but nothing tells anyone it happened. This does. The live binding stays silent."""
    estate = _Estate(tmp_path)
    wh_svc.bind_namespace(estate.control_root, {}, "stranded_ns", "wh-gone", "s3://bkt-gone")
    report = estate.run(monkeypatch)
    assert [(d.top_ns, d.warehouse_id) for d in report.dangling_bindings] == [("stranded_ns", "wh-gone")]


# --------------------------------------------------------------------------- #
# degradation + honesty about partial scans
# --------------------------------------------------------------------------- #


def test_an_openfga_outage_degrades_only_the_categories_that_needed_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A drift report that 500s on one bad store tells you nothing about the other categories. FGA down
    → EVERY authz-derived category reports UNAVAILABLE **with the reason**, and the storage-only ones
    still find their drift. Stated as "the categories that read FGA" rather than a hand-kept list, so
    adding a category cannot leave it silently unasserted."""
    estate = _Estate(tmp_path)
    estate.fga.error = ServiceUnavailableError("openfga down")
    _Estate.namespace_dir(tmp_path, "legacy_ns")
    wh_svc.bind_namespace(estate.control_root, {}, "stranded_ns", "wh-gone", "s3://bkt-gone")
    estate.s3 = _FakeS3([_PRIMARY_BUCKET, "bkt-a", "bkt-orphan"])

    report = estate.run(monkeypatch)
    assert {u.category for u in report.unavailable} == _FGA_DERIVED_CATEGORIES
    assert all("ServiceUnavailableError" in u.reason for u in report.unavailable)
    assert set(report.counts) == {"unbound_namespaces", "orphan_buckets", "dangling_bindings"}
    assert [n.namespace for n in report.unbound_namespaces] == ["legacy_ns"]
    assert [b.bucket for b in report.orphan_buckets] == ["bkt-orphan"]
    assert [d.top_ns for d in report.dangling_bindings] == ["stranded_ns"]
    assert report.total == 3


def test_a_dead_bucket_listing_degrades_only_orphan_buckets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The symmetric case from the other side: object storage forbidden, authz and the registries fine."""
    estate = _Estate(tmp_path)
    estate.s3 = _FakeS3([], error=PermissionError("ListBuckets forbidden"))
    report = estate.run(monkeypatch)
    assert [u.category for u in report.unavailable] == ["orphan_buckets"]
    assert "PermissionError" in report.unavailable[0].reason
    assert "orphan_buckets" not in report.counts
    assert set(report.counts) == set(mod.CATEGORIES) - {"orphan_buckets"}


def test_no_scan_is_capped_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated scan that reads as "all clear" is worse than no scan. A tuple cursor that never
    ends, a paginated bucket listing, and an unreadable registry record each land in `incomplete` —
    while the categories built on them still report what they DID see."""
    estate = _Estate(tmp_path)
    estate.fga.endless = True
    estate.s3 = _FakeS3([_PRIMARY_BUCKET, "bkt-a"], truncated=True)
    Path(estate.control_root.removeprefix("file://"), "_projects", "zzz-corrupt.json").write_text("{truncated")

    report = estate.run(monkeypatch)
    reasons = {scan.source: scan.reason for scan in report.incomplete}
    assert "fga:project" in reasons and "page ceiling" in reasons["fga:project"]
    assert "fga:warehouse" in reasons
    assert "storage:buckets" in reasons and "ONE PAGE" in reasons["storage:buckets"]
    assert "registry:projects" in reasons and "unreadable" in reasons["registry:projects"]
    # ...and the run is still a full report, not a failure: every category was reached.
    assert set(report.counts) == set(mod.CATEGORIES)


def test_a_missing_control_root_is_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A control root with nothing on it yet (a fresh estate) lists as empty, not as an error — the
    reconciler must be safe to schedule from day one."""
    fga = _FakeFga()
    fga.install(monkeypatch)
    report = asyncio.run(
        mod.reconcile(
            _settings(),
            cast(Any, object()),
            warehouses_enabled=True,
            control_root=f"file://{tmp_path / 'nothing-here'}",
            namespace_root=f"file://{tmp_path / 'nothing-here'}",
            bucket_client=_FakeS3([]),
        )
    )
    assert report.total == 0
    assert report.unavailable == []


def test_an_unwired_openfga_client_is_reported_not_assumed_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`client=None` is a scan that cannot run, NOT an estate with no ghosts. Silently empty authz
    categories would let a report certify an estate it never looked at."""
    estate = _Estate(tmp_path)
    estate.fga.install(monkeypatch)
    report = asyncio.run(
        mod.reconcile(
            _settings(),
            None,
            warehouses_enabled=True,
            control_root=estate.control_root,
            namespace_root=estate.namespace_root,
            bucket_client=estate.s3,
        )
    )
    assert {u.category for u in report.unavailable} == _FGA_DERIVED_CATEGORIES
    assert all("no OpenFGA client" in u.reason for u in report.unavailable)


# --------------------------------------------------------------------------- #
# the report-only guarantee, asserted mechanically
# --------------------------------------------------------------------------- #

#: Call-name prefixes that could write to a store. Matched against the AST's called NAME (a plain call
#: or the attribute of a method call) rather than the source text — a regex hits the word inside a
#: docstring, and this module's docstrings are full of them, which is how a gate stops being trusted.
#:
#: The Lance verbs (``compact_files`` / ``cleanup_old_versions`` / ``optimize_indices``) are on this
#: list because they are what the SIBLING module in this very package calls — the single most likely
#: thing for someone to copy in here, and the most destructive: ``cleanup_old_versions`` deletes
#: history irreversibly. A prefix list that omits its own neighbour's calls is a gate that reads as
#: strict and is not; :func:`test_the_mutating_call_gate_is_not_vacuous` pins that it catches them.
_MUTATING_CALL_PREFIXES = (
    "add_",
    "alter",
    "cleanup",
    "commit",
    "compact",
    "copy_",
    "create",
    "delete",
    "drop",
    "insert",
    "merge_",
    "move",
    "open_output",
    "optimize",
    "overwrite",
    "purge",
    "put_",
    "remove",
    "rename",
    "restore",
    "revoke",
    "rmtree",
    "set_",
    "truncate",
    "unbind",
    "unlink",
    "update",
    "upload",
    "write",
)


def _called_names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def test_the_module_contains_no_mutating_call() -> None:
    """NOTHING DELETES. NOTHING MUTATES — enforced, not asserted in prose.

    A reclaimer that deletes on its first run against an unvalidated rule eats live data, so the
    read-only property has to survive the next person editing this module. Any call whose name could
    write fails here.
    """
    path = Path(mod.__file__)
    offenders = sorted(name for name in _called_names(path.read_text()) if name.lower().startswith(_MUTATING_CALL_PREFIXES))
    assert offenders == [], f"{path.name} calls {offenders} — the reconciler is REPORT-ONLY and must never mutate a store"


def test_the_mutating_call_gate_is_not_vacuous() -> None:
    """A gate nobody has seen fire is a gate nobody knows works — so fire it at the sibling module.

    ``compaction.services.optimize`` is the reconciler's neighbour and it DOES mutate: it compacts
    fragments and deletes old versions. Pointing the same checker at it must produce offenders. This
    is the assertion that would have caught the prefix list missing ``cleanup_old_versions`` — the
    most destructive call in this package — while ``test_the_module_contains_no_mutating_call`` stayed
    green and looked like proof.
    """
    sibling = Path(mod.__file__).with_name("optimize.py")
    offenders = sorted(name for name in _called_names(sibling.read_text()) if name.lower().startswith(_MUTATING_CALL_PREFIXES))
    assert "cleanup_old_versions" in offenders, f"the gate does not catch the sibling sweep's own destructive calls: {offenders}"
    assert "compact_files" in offenders


def _tree_fingerprint(root: Path) -> dict[str, str]:
    """Every file under ``root`` by relative path → a hash of its bytes."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def test_a_full_run_leaves_every_store_byte_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The behavioural half of the same guarantee, on a DRIFTED estate (the state a reclaimer would be
    tempted to fix): the control root and the data root come out byte-identical, and the FGA/S3 doubles
    raise on every write verb, so a mutation is a failure rather than an unnoticed success."""
    estate = _Estate(tmp_path)
    estate.fga.tuples["project:phantom"] = [("user:mallory", "admin")]
    _Estate.namespace_dir(tmp_path, "legacy_ns")
    wh_svc.bind_namespace(estate.control_root, {}, "stranded_ns", "wh-gone", "s3://bkt-gone")
    proj_svc.put_project(estate.control_root, {}, {"id": "orphaned", "created_at": "t", "created_by": "gone", "protected": "false"})
    estate.s3 = _FakeS3([_PRIMARY_BUCKET, "bkt-a", "bkt-orphan"])

    before = {name: _tree_fingerprint(tmp_path / name) for name in ("control", "data")}
    report = estate.run(monkeypatch)
    after = {name: _tree_fingerprint(tmp_path / name) for name in ("control", "data")}

    assert report.total == 5, "the fixture must actually BE drifted, or 'nothing changed' proves nothing"
    assert after == before
