"""The cross-store drift reconciler — a REPORT, beside the sweep (`open_hierarchy_lifecycle.md` Decision 4).

**NOTHING DELETES. NOTHING MUTATES.** Every path in this module is read-only, and that is the point:
a reclaimer that deletes on its first run against an unvalidated rule eats live data. The output is a
typed report a later, separately-decided reclaimer can consume — once the report has run clean on a
real estate.

Three stores must agree about the hierarchy (project > warehouse > namespace > table) and NOTHING
checks that they do:

* **OpenFGA** says WHO. It can hold tuples for a tenant the catalog has never heard of (the live estate
  held three such projects), and it can hold none at all for one that exists.
* **The registries** — JSON records on the control root (``_projects/``, ``_warehouses/``,
  ``_warehouses/bindings/``) — say WHAT EXISTS. Since Decision 1 this is the definition of existence.
* **Object storage** holds the BYTES. A warehouse delete removes the record; the bucket survives unless
  someone explicitly asked to purge it, so a bucket can outlive the record that named it.

Seven detectors, one per drift category, each independently testable and each degrading on its own:
a category whose source is unreachable reports UNAVAILABLE **with the reason** while the other six
still report. A drift report that 500s on one bad store tells you nothing about the other six. Nor is
anything capped silently — a truncated scan that reads as "all clear" is worse than no scan, so every
page ceiling, unreadable record and paginated listing lands in :attr:`ReconcileReport.incomplete`.

Why the registries are re-read here rather than imported from the catalog: compaction has no catalog
client BY DESIGN, and declaring one would put the whole catalog closure in the compaction image (the
`tests/unit/test_declared_dependencies.py` contract). This is the same catalog↔compaction shape as
:mod:`service_kit.lakehouse.maintenance_policies` and :mod:`service_kit.lakehouse.warehouse_registry`:
one service writes the records, another reads them straight off the bucket. The record layout is the
shared contract, and the reader below is deliberately the *tolerant* half of it — a record it cannot
parse is reported as an incomplete scan, never allowed to void a category.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

import lance
import pyarrow.fs as pafs
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from maintenance.core.config import MaintenanceSettings, shared_lance_session
from maintenance.services.optimize import discover_datasets
from maintenance.services.orphans import OrphanFile, scan_datasets
from service_kit.governed import fga
from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)

#: The registry prefixes, byte-identical to what the catalog writes them at. Bindings live UNDER the
#: warehouse prefix, so every warehouse listing here is non-recursive (a recursive one would read the
#: bindings as malformed warehouse records).
_PROJECTS_PREFIX = "_projects"
_WAREHOUSES_PREFIX = "_warehouses"
_BINDINGS_PREFIX = "_warehouses/bindings"

#: The FGA object the estate-admin gates check against (`Settings.fga_root_object`'s default). It is the
#: platform's own root, NOT a tenant warehouse, so it has no registry record BY DESIGN — without this
#: exclusion every run would report the estate root as a ghost warehouse forever, and a report that
#: always contains a known-good finding is a report nobody reads.
DEFAULT_FGA_ROOT_OBJECT = "warehouse:lance_catalog"

#: Page ceiling for one object-type tuple scan. An estate's project/warehouse tuple count is small, so
#: this is unreachable in practice; it exists so a continuation-token bug cannot spin the report
#: forever. Hitting it is REPORTED as an incomplete scan, never silently truncated.
_MAX_TUPLE_PAGES = 200
_TUPLE_PAGE_SIZE = 100

#: The namespace spec's own object index, one per root: ``object_id``/``object_type`` rows recording
#: every namespace and table. It is the ONLY place a namespace exists — namespaces are not directories
#: — so the unbound-namespace scan reads this rather than listing prefixes.
#: Public because the #79 purge re-reads the SAME index to re-check liveness before it deletes anything.
#: One constant rather than two copies: a purge looking at the wrong path would find no live objects and
#: happily destroy every one of them.
MANIFEST_DIR = "__manifest"
_MANIFEST_NAMESPACE_TYPE = "namespace"

#: The drift categories, in report order. `counts` carries exactly the ones that were CHECKED — an
#: unavailable or skipped category is absent rather than zero, so a 0 can never read as "clean".
CATEGORIES: tuple[str, ...] = (
    "ghost_projects",
    "ghost_warehouses",
    "unreferenced_projects",
    "unbound_namespaces",
    "orphan_buckets",
    "dangling_bindings",
    "orphaned_annotation_tasks",
    "orphan_files",
)

#: Categories that are REPORTED but do not gate the #79 purge — counted, named, visible in every
#: report, and excluded from `total`.
#:
#: THE GATE'S JOB IS TO PROVE THE STORAGE STATE IS UNDERSTOOD before the purge deletes expired trash
#: bytes. Neither of these is a storage fact, and neither can change which bytes the purge touches:
#:
#:   * `orphaned_annotation_tasks` — an FGA edge in the ANNOTATOR plane naming a retired tenant.
#:     Annotation tasks deliberately do not block a project delete, so retiring a tenant legitimately
#:     leaves the edge behind; the task is not dangerous, it is UNREACHABLE (see
#:     :class:`OrphanedAnnotationTask`). No bytes are implicated.
#:   * `unbound_namespaces` — a top-level namespace with no warehouse BINDING record. It is a registry
#:     gap about routing, not about what exists on disk.
#:
#: And neither has a door. No endpoint in the product clears an orphaned annotation task or binds a
#: legacy namespace, so gating on them did not make the purge safer — it made the gate permanently
#: unsatisfiable. MEASURED 2026-08-16: the estate sat at 3 and 5 respectively with every other category
#: at zero, so `report_is_clean` could never return None by any action an operator could take. An
#: unreachable gate is not a safety property; it is a feature that cannot be switched on, and it hides
#: the categories that DO matter behind a total that never falls.
#:
#: Adding a door for either is still worthwhile and is the strictly better fix — the drift is real. The
#: two halves differ in risk and should not be built together: revoking an orphaned annotation task
#: DELETES unreachable tuples, while binding a legacy namespace WRITES a registry record for live data.
NON_GATING_CATEGORIES: frozenset[str] = frozenset({"orphaned_annotation_tasks", "unbound_namespaces"})


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


class GhostObject(BaseModel):
    """An FGA object carrying tuples that no registry record names — authz for a tenant that does not exist."""

    kind: str
    id: str
    fga_object: str
    #: How many tuples hang off it — the size of the grant surface a reclaimer would revoke.
    tuples: int


class UnreferencedProject(BaseModel):
    """A project record with NO tuples at all: nobody can administer it, so nobody can delete it either."""

    id: str


class UnboundNamespace(BaseModel):
    """A top-level namespace on the SHARED default root with no warehouse binding (pre-rule legacy)."""

    namespace: str
    #: The root it was found on — the shared default bucket, which is exactly where an unbound
    #: namespace silently resolves to.
    root: str


class OrphanBucket(BaseModel):
    """A bucket no warehouse record claims — the "deleted without purge" residue."""

    bucket: str


class DanglingBinding(BaseModel):
    """A namespace→warehouse binding pointing at a warehouse_id with no record."""

    top_ns: str
    warehouse_id: str


class OrphanedAnnotationTask(BaseModel):
    """An annotation task whose ``tenant`` edge names a catalog project that no longer exists.

    Annotation tasks are NOT warehouses: one catalog project holds many, they live in the annotator
    plane, and they deliberately do not block a project delete. So retiring a tenant legitimately
    leaves this edge behind — the annotator's ``owner``/``viewer`` rungs derive from
    ``admin``/``member from tenant``, and with the tenant's tuples revoked there is no principal left
    to cascade from. The task is not dangerous, it is UNREACHABLE, and nothing else would ever say so.
    """

    annotation_project: str
    #: The tenant it still points at — gone from the project registry.
    tenant: str


class CategoryUnavailable(BaseModel):
    """A category that could NOT be checked because a store it needs was unreachable."""

    category: str
    reason: str


class CategorySkipped(BaseModel):
    """A category deliberately not checked — either because its rule does not apply, or because
    nobody looked. Those are different facts and `report_is_clean` must be able to tell them apart.

    ``coverage_gap`` carries the distinction as DATA rather than leaving the gate to infer it from
    the category name. Two skips shipped here and they are opposites:

      * ``unbound_namespaces`` when warehouses are off — the rule genuinely does not apply. There are
        no bindings, so nothing was missed and nothing can be found. NOT a gap.
      * ``orphan_files`` when the scan is off — the rule applies and we chose not to run it. The file
        layer was never inspected. A GAP, and the #128a defect was that the gate could not see it.

    Defaults False so a does-not-apply skip added later cannot silently block every purge in the
    estate; a genuine gap has to SAY so, which is the whole job of the flag.
    """

    category: str
    reason: str
    coverage_gap: bool = False


class IncompleteScan(BaseModel):
    """A source that answered PARTIALLY — a page ceiling, a paginated listing, an unreadable record.

    Recorded separately from a failure because the categories built on it still report: they may
    under- or over-report, and this names which input to distrust.
    """

    source: str
    reason: str


class ReconcileReport(BaseModel):
    """One cross-store drift scan. Machine-readable on purpose — a later reclaimer consumes this shape.

    ``counts`` holds one entry per CHECKED category and ``total`` their sum; an unavailable or skipped
    category appears in neither, so "absent" and "zero findings" stay distinguishable.
    """

    checked_at: str
    ghost_projects: list[GhostObject] = Field(default_factory=list)
    ghost_warehouses: list[GhostObject] = Field(default_factory=list)
    unreferenced_projects: list[UnreferencedProject] = Field(default_factory=list)
    unbound_namespaces: list[UnboundNamespace] = Field(default_factory=list)
    orphan_buckets: list[OrphanBucket] = Field(default_factory=list)
    dangling_bindings: list[DanglingBinding] = Field(default_factory=list)
    orphaned_annotation_tasks: list[OrphanedAnnotationTask] = Field(default_factory=list)
    #: Files under a dataset prefix that no LIVE version references (the reclamation gap).
    orphan_files: list[OrphanFile] = Field(default_factory=list)
    unavailable: list[CategoryUnavailable] = Field(default_factory=list)
    skipped: list[CategorySkipped] = Field(default_factory=list)
    incomplete: list[IncompleteScan] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    total: int = 0


# --------------------------------------------------------------------------- #
# Read-only source readers
# --------------------------------------------------------------------------- #


def _reason(source: str, exc: BaseException) -> str:
    """A failure reason that names the source AND the exception class — "unavailable" alone is not a lead."""
    return f"{source}: {type(exc).__name__}: {exc}"


async def _read_async[T](source: str, awaitable: Awaitable[T]) -> tuple[T | None, str | None]:
    """Await one source, converting any failure into a reason string. Never raises — a dead store
    degrades its own categories and nothing else."""
    try:
        return (await awaitable), None
    except Exception as exc:
        log.warning("reconcile_source_unavailable", extra={"source": source, "error": str(exc)})
        return None, _reason(source, exc)


async def _read_blocking[T](source: str, fn: Callable[..., T], *args: Any) -> tuple[T | None, str | None]:
    """The blocking twin of :func:`_read_async` — the registry/bucket listings are blocking IO, so they
    run in the threadpool exactly like the sweep's."""
    return await _read_async(source, run_in_threadpool(fn, *args))


def _list_json_records(root: str, storage_options: StorageOptions, prefix: str, required: tuple[str, ...]) -> tuple[list[dict[str, str]], list[str]]:
    """Every readable JSON record directly under ``<root>/<prefix>``, plus a reason per record SKIPPED.

    Non-recursive, so ``_warehouses`` does not swallow ``_warehouses/bindings``. Per-record tolerance
    matches the catalog's own listings — but here a skip is not merely survived, it is REPORTED: a
    record this cannot read is a tenant it cannot see, and a category computed without it may name a
    live object as drift.
    """
    fs, base = fs_and_base(root, storage_options)
    records: list[dict[str, str]] = []
    skipped: list[str] = []
    for info in fs.get_file_info(pafs.FileSelector(f"{base}/{prefix}", allow_not_found=True, recursive=False)):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                record = json.loads(stream.readall().decode("utf-8"))
        except Exception as exc:
            skipped.append(f"{info.path} is unreadable ({type(exc).__name__}: {exc})")
            continue
        if isinstance(record, dict) and all(record.get(key) for key in required):
            records.append(record)
        else:
            skipped.append(f"{info.path} is missing one of {list(required)}")
    return records, skipped


def _top_level_namespaces(root: str, storage_options: StorageOptions, delimiter: str) -> list[str]:
    """The top-level namespaces recorded on ``root``, sorted.

    Read from the namespace MANIFEST (``<root>/__manifest``, the spec's own object index), not from
    the directory listing. A namespace is not a directory: ``create_namespace`` on the ``dir`` impl —
    what the chart runs (``LANCE_REST_IMPL=dir``) — writes a row and nothing else, while a TABLE lands
    flat at the root as ``<uuid>_<ns><delimiter><table>/``. Scanning directories therefore finds every
    table and no namespace at all, which is the failure direction that reports an estate CLEAN.

    ``object_type`` separates namespaces from tables and the delimiter separates top-level from nested
    (a child is recorded as ``parent<delimiter>child``). A root with no manifest yet is a fresh estate,
    not an error: it yields ``[]``.

    This reads the SHARED DEFAULT root deliberately: a warehouse-BOUND namespace is created against its
    warehouse's own bucket and is recorded in THAT bucket's manifest, so the shared root's manifest is
    precisely where an unbound one lands.
    """
    fs, base = fs_and_base(root, storage_options)
    # Probe first so the fresh-estate case never rides an exception: an absent dataset raises, and a
    # raise here would report the category unavailable on a brand-new estate.
    if fs.get_file_info(f"{base}/{MANIFEST_DIR}/_versions").type != pafs.FileType.Directory:
        return []
    manifest_uri = f"s3://{base}/{MANIFEST_DIR}" if root.startswith("s3://") else f"{base}/{MANIFEST_DIR}"
    dataset = lance.dataset(manifest_uri, storage_options=storage_options, session=shared_lance_session())  # ty: ignore[invalid-argument-type] — stub lacks session=, runtime verified
    table = dataset.to_table(columns=["object_id", "object_type"])
    return sorted(
        {
            str(object_id)
            for object_id, object_type in zip(table.column("object_id").to_pylist(), table.column("object_type").to_pylist(), strict=True)
            if object_type == _MANIFEST_NAMESPACE_TYPE and object_id and delimiter not in str(object_id)
        }
    )


def _top_level_namespaces_across(roots: list[str], storage_options: StorageOptions, delimiter: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(namespace, root)`` pairs across EVERY root, plus the roots that could not be read (F3.3).

    The scan read exactly ONE root — the shared default bucket — while a warehouse-enabled estate puts
    each tenant's namespaces in that tenant's OWN bucket. So the whole per-warehouse namespace layer
    was outside the reconciler's field of view: not merely unrepaired, UNREPORTED, and a zero in a
    drift report is read as "checked and clean". That report is also the gate `purge_expired_trash`
    consults before deleting anything, so the blind spot let it certify an estate it had never looked at.

    The root travels WITH each namespace rather than being applied to the batch, because
    `UnboundNamespace.root` is what tells an operator which bucket to go and look in — and with more
    than one root in play, a batch-level root would be wrong for every finding but one.

    PER-ROOT TOLERANCE, and the alternative was measured and rejected. Raising on the first unreadable
    root turns one unreachable tenant bucket — a deleted warehouse, revoked credentials, a quarantined
    tenant — into a `CategoryUnavailable`, which blocks `report_is_clean` and with it the purge, for
    the WHOLE estate. So a failed root is reported as an `IncompleteScan` and the readable ones still
    report, which is exactly the rule the orphan scan already follows: name the input to distrust
    rather than either hiding the gap or stopping everything.
    """
    found: set[tuple[str, str]] = set()
    unreadable: list[tuple[str, str]] = []
    for root in roots:
        try:
            # `|=`, not `.update(...)`: the module's read-only AST guard bans any call named `update`
            # and cannot tell a set method from a store write. Augmented assignment is not a call.
            found |= {(name, root) for name in _top_level_namespaces(root, storage_options, delimiter)}
        except Exception as exc:  # noqa: BLE001 — one unreachable bucket must not blind the whole scan
            unreadable.append((root, f"{type(exc).__name__}: {exc}"))
    return sorted(found), unreadable


def _list_bucket_names(client: Any) -> tuple[list[str], str | None]:
    """Every bucket the credentials can see, sorted, plus a truncation reason when the listing paged.

    S3 ListBuckets grew pagination; a page silently read as the whole estate would make every bucket
    past it look claimed, which is the direction that HIDES drift.
    """
    response = client.list_buckets()
    names = sorted(str(b["Name"]) for b in (response.get("Buckets") or []) if b.get("Name"))
    truncated = response.get("ContinuationToken") or response.get("NextContinuationToken") or response.get("IsTruncated")
    if truncated:
        return names, f"ListBuckets returned a continuation cursor — these {len(names)} bucket(s) are ONE PAGE, not the estate"
    return names, None


class TupleScan(BaseModel):
    """One whole-store tuple read, bucketed by object type — the input to every authz category."""

    #: ``{object_type: {object_id: tuple_count}}``
    counts_by_type: dict[str, dict[str, int]] = Field(default_factory=dict)
    #: ``(annotation_project_id, tenant_project_id)`` for each ``annotation_project#tenant`` edge.
    annotation_tenants: list[tuple[str, str]] = Field(default_factory=list)


async def _scan_tuples(client: Any) -> tuple[TupleScan, str | None]:
    """Read the WHOLE store once, paged to the end, and bucket every tuple by its object type.

    **One unfiltered read, not one bare-type read per category, and that is a correctness fix rather
    than an optimization.** OpenFGA rejects a Read whose ``tuple_key`` is present but carries neither
    an object id nor a user: ``the 'tuple_key' field was provided but the object type field is
    required and both the object id and user cannot be empty`` (HTTP 400). A bare-type filter
    (``obj="project:"``) is exactly that shape, so every authz category built on one failed against a
    real server while passing against a double that accepted the filter. Measured live 2026-08-04.

    The wrapper reports that 400 as ``ServiceUnavailableError``, so the failure presented as "the
    authorization service is down" — permanently, on a healthy server. That is why the fix is here and
    not a retry.

    Reading everything is affordable BY THE SHAPE OF THIS DATA: these are governance tuples, written
    at admin frequency, and one page covers a real estate (the live cluster returned 22 in a single
    page). It is also strictly fewer round-trips than the four scans it replaces.

    Returns a truncation reason — never a silently short answer — if the page ceiling is reached with
    a cursor outstanding: a short scan would invent ghosts on the registry side and hide them on the
    FGA side, in the same run.
    """
    scan = TupleScan()
    token: str | None = None
    for _ in range(_MAX_TUPLE_PAGES):
        page, token = await fga.read_tuples(client, page_size=_TUPLE_PAGE_SIZE, continuation_token=token)
        for item in page:
            obj_type, _, obj_id = str(item.object).partition(":")
            if not obj_id:
                continue
            bucket = scan.counts_by_type.setdefault(obj_type, {})
            bucket[obj_id] = bucket.get(obj_id, 0) + 1
            if obj_type == "annotation_project" and item.relation == "tenant":
                tenant_type, _, tenant_id = str(item.user).partition(":")
                if tenant_type == "project" and tenant_id:
                    scan.annotation_tenants.append((obj_id, tenant_id))
        if not token:
            return scan, None
    return scan, f"the tuple scan hit its {_MAX_TUPLE_PAGES}-page ceiling with a cursor outstanding"


# --------------------------------------------------------------------------- #
# The detectors — pure, one per category
# --------------------------------------------------------------------------- #


def _ghosts(kind: str, tuple_counts: Mapping[str, int], record_ids: set[str], exclude: set[str]) -> list[GhostObject]:
    """FGA objects of ``kind`` holding tuples that no registry record names."""
    return [
        GhostObject(kind=kind, id=obj_id, fga_object=f"{kind}:{obj_id}", tuples=count)
        for obj_id, count in sorted(tuple_counts.items())
        if obj_id not in record_ids and obj_id not in exclude
    ]


def _unreferenced_projects(record_ids: set[str], tuple_counts: Mapping[str, int]) -> list[UnreferencedProject]:
    """Project records with no tuples at all — an undeletable tenant is drift too."""
    return [UnreferencedProject(id=project_id) for project_id in sorted(record_ids) if not tuple_counts.get(project_id)]


def _orphaned_annotation_tasks(edges: Iterable[tuple[str, str]], project_ids: set[str]) -> list[OrphanedAnnotationTask]:
    """Annotation tasks whose tenant edge names a project the registry no longer has.

    The expected residue of a project delete, which refuses on warehouses but NOT on annotation tasks
    (one tenant holds many, and they are not a rung of project > warehouse > namespace > table). This
    is the category that exists precisely because the delete door was right to let it happen.
    """
    return [OrphanedAnnotationTask(annotation_project=task_id, tenant=tenant_id) for task_id, tenant_id in sorted(set(edges)) if tenant_id not in project_ids]


def _unbound_namespaces(namespaces: Iterable[tuple[str, str]], bound: set[str]) -> list[UnboundNamespace]:
    """Top-level namespaces that no binding claims, each carrying the root it was actually found on."""
    return [UnboundNamespace(namespace=name, root=root) for name, root in sorted(namespaces) if name not in bound]


def _orphan_buckets(buckets: Iterable[str], claimed: set[str], platform: set[str]) -> list[OrphanBucket]:
    """Buckets no warehouse record claims and no platform role explains.

    Over-reporting is the SAFE direction for a report: an unrecognised platform bucket shows up as a
    finding a human dismisses, whereas an over-broad exemption hides a real orphan permanently.
    """
    return [OrphanBucket(bucket=bucket) for bucket in sorted(buckets) if bucket not in claimed and bucket not in platform]


def _dangling_bindings(bindings: Iterable[Mapping[str, str]], warehouse_ids: set[str]) -> list[DanglingBinding]:
    """Bindings pointing at a warehouse_id with no record — a namespace routed at nothing."""
    return sorted(
        (DanglingBinding(top_ns=str(b["top_ns"]), warehouse_id=str(b["warehouse_id"])) for b in bindings if str(b["warehouse_id"]) not in warehouse_ids),
        key=lambda d: (d.top_ns, d.warehouse_id),
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


class Sources(BaseModel):
    """Every store this report reads, each paired with the reason it FAILED instead of a value.

    One field per source rather than one try/except per category, so a dead store degrades exactly the
    categories that needed it and no others — which is the whole difference between this and a report
    that 500s.
    """

    #: ONE whole-store tuple scan (see `_scan_tuples`), bucketed by object type. All four authz
    #: categories read this same snapshot, so they cannot disagree about the store's contents.
    tuples: TupleScan | None = None
    tuples_error: str | None = None
    project_records: list[dict[str, str]] | None = None
    project_records_error: str | None = None
    warehouse_records: list[dict[str, str]] | None = None
    warehouse_records_error: str | None = None
    bindings: list[dict[str, str]] | None = None
    bindings_error: str | None = None
    namespaces: list[tuple[str, str]] | None = None
    namespaces_error: str | None = None
    buckets: list[str] | None = None
    buckets_error: str | None = None
    incomplete: list[IncompleteScan] = Field(default_factory=list)


#: Said when FGA is not wired into the process at all — distinct from an outage, and the distinction
#: matters: with no client there is nothing wrong with the estate, only with the scan.
_FGA_ABSENT = "no OpenFGA client is wired into this process, so authz-vs-registry drift cannot be checked"

#: Said when the warehouse feature is off. With warehouses disabled an unbound namespace is CORRECT —
#: reporting it would train the reader to ignore the category before it ever means anything.
_WAREHOUSES_OFF = "warehouses are disabled — every namespace resolving to the shared default root is correct, not drift"


def _bucket_of(root_uri: str) -> str | None:
    """The bucket a root URI addresses, or ``None`` for a local/file root (which owns no bucket)."""
    if not root_uri.startswith("s3://"):
        return None
    return root_uri[len("s3://") :].strip("/").split("/")[0] or None


async def _fga_source(sources: Sources, client: Any) -> tuple[TupleScan | None, str | None]:
    """ONE whole-store tuple scan feeding all four authz categories.

    Previously four separate bare-type scans, one per category — a request shape OpenFGA rejects with
    a 400 (see :func:`_scan_tuples`). Folding them into one read also means the four categories can no
    longer disagree about the store's contents, because they now read the same snapshot.
    """
    if client is None:
        return None, _FGA_ABSENT
    result, error = await _read_async("fga:tuples", _scan_tuples(client))
    if result is None:
        return None, error
    scan, truncation = result
    if truncation:
        sources.incomplete.append(IncompleteScan(source="fga:tuples", reason=truncation))
    return scan, None


async def _registry_source(
    sources: Sources,
    source_name: str,
    control_root: str,
    storage_options: StorageOptions,
    prefix: str,
    required: tuple[str, ...],
) -> tuple[list[dict[str, str]] | None, str | None]:
    """One registry listing, folding its per-record skips into ``sources.incomplete``."""
    listing, error = await _read_blocking(source_name, _list_json_records, control_root, storage_options, prefix, required)
    if listing is None:
        return None, error
    records, skipped = listing
    sources.incomplete.extend(IncompleteScan(source=source_name, reason=reason) for reason in skipped)
    return records, None


async def _bucket_source(sources: Sources, bucket_client: Any) -> tuple[list[str] | None, str | None]:
    """The bucket listing, folding its pagination note into ``sources.incomplete``."""
    if bucket_client is None:
        return None, "no S3 client is wired into this process, so bucket residue cannot be checked"
    listing, error = await _read_blocking("storage:buckets", _list_bucket_names, bucket_client)
    if listing is None:
        return None, error
    names, truncation = listing
    if truncation:
        sources.incomplete.append(IncompleteScan(source="storage:buckets", reason=truncation))
    return names, None


async def load_sources(
    *,
    client: Any,
    control_root: str,
    namespace_root: str,
    storage_options: StorageOptions,
    bucket_client: Any,
    delimiter: str,
) -> Sources:
    """Read all seven sources, independently. Nothing here raises."""
    sources = Sources()
    sources.tuples, sources.tuples_error = await _fga_source(sources, client)
    sources.project_records, sources.project_records_error = await _registry_source(
        sources, "registry:projects", control_root, storage_options, _PROJECTS_PREFIX, ("id",)
    )
    sources.warehouse_records, sources.warehouse_records_error = await _registry_source(
        sources, "registry:warehouses", control_root, storage_options, _WAREHOUSES_PREFIX, ("id", "bucket", "project")
    )
    sources.bindings, sources.bindings_error = await _registry_source(
        sources, "registry:bindings", control_root, storage_options, _BINDINGS_PREFIX, ("top_ns", "warehouse_id")
    )
    # EVERY root, not just the shared one (diff2 F3.3). Reuses the warehouse registry already loaded
    # above rather than reading it again — two reads would be two answers, and the second could
    # disagree with the one `dangling_bindings` was computed from.
    warehouse_roots = sorted({str(r["root_uri"]) for r in (sources.warehouse_records or []) if r.get("root_uri")})
    namespace_roots = [namespace_root, *(r for r in warehouse_roots if r != namespace_root)]
    scanned, sources.namespaces_error = await _read_blocking("catalog:namespaces", _top_level_namespaces_across, namespace_roots, storage_options, delimiter)
    if scanned is not None:
        sources.namespaces, unreadable_roots = scanned
        sources.incomplete.extend(IncompleteScan(source=f"catalog:namespaces:{root}", reason=reason) for root, reason in unreadable_roots)
    sources.buckets, sources.buckets_error = await _bucket_source(sources, bucket_client)
    return sources


def _by_type(sources: Sources, object_type: str) -> dict[str, int]:
    """``{object_id: tuple_count}`` for one object type out of the single whole-store scan."""
    return (sources.tuples.counts_by_type.get(object_type, {}) if sources.tuples else {}) or {}


def _first(*reasons: str | None) -> str | None:
    """The first source failure among a category's inputs — the reason that category cannot be checked."""
    return next((reason for reason in reasons if reason), None)


def build_report(
    sources: Sources,
    *,
    warehouses_enabled: bool,
    platform_buckets: set[str],
    fga_root_object: str,
) -> ReconcileReport:
    """Run every detector whose inputs are present; record the rest as unavailable or skipped."""
    report = ReconcileReport(checked_at=datetime.now(UTC).isoformat(), incomplete=list(sources.incomplete))
    project_ids = {str(r["id"]) for r in sources.project_records or []}
    warehouse_ids = {str(r["id"]) for r in sources.warehouse_records or []}
    root_type, _, root_id = fga_root_object.partition(":")

    if (reason := _first(sources.tuples_error, sources.project_records_error)) is not None:
        report.unavailable.append(CategoryUnavailable(category="ghost_projects", reason=reason))
    else:
        report.ghost_projects = _ghosts("project", _by_type(sources, "project"), project_ids, {root_id} if root_type == "project" else set())
        report.counts["ghost_projects"] = len(report.ghost_projects)

    if (reason := _first(sources.tuples_error, sources.warehouse_records_error)) is not None:
        report.unavailable.append(CategoryUnavailable(category="ghost_warehouses", reason=reason))
    else:
        report.ghost_warehouses = _ghosts("warehouse", _by_type(sources, "warehouse"), warehouse_ids, {root_id} if root_type == "warehouse" else set())
        report.counts["ghost_warehouses"] = len(report.ghost_warehouses)

    if (reason := _first(sources.project_records_error, sources.tuples_error)) is not None:
        report.unavailable.append(CategoryUnavailable(category="unreferenced_projects", reason=reason))
    else:
        report.unreferenced_projects = _unreferenced_projects(project_ids, _by_type(sources, "project"))
        report.counts["unreferenced_projects"] = len(report.unreferenced_projects)

    if not warehouses_enabled:
        report.skipped.append(CategorySkipped(category="unbound_namespaces", reason=_WAREHOUSES_OFF))
    # `warehouse_records_error` joins the guard: that registry is what ENUMERATES the roots to scan,
    # so without it the scan silently covers the shared root alone — which is the pre-F3.3 blind spot
    # reappearing as a clean report rather than as an unavailable category.
    elif (reason := _first(sources.namespaces_error, sources.bindings_error, sources.warehouse_records_error)) is not None:
        report.unavailable.append(CategoryUnavailable(category="unbound_namespaces", reason=reason))
    else:
        bound = {str(b["top_ns"]) for b in sources.bindings or []}
        report.unbound_namespaces = _unbound_namespaces(sources.namespaces or [], bound)
        report.counts["unbound_namespaces"] = len(report.unbound_namespaces)

    if (reason := _first(sources.buckets_error, sources.warehouse_records_error)) is not None:
        report.unavailable.append(CategoryUnavailable(category="orphan_buckets", reason=reason))
    else:
        claimed = {str(r["bucket"]) for r in sources.warehouse_records or []}
        report.orphan_buckets = _orphan_buckets(sources.buckets or [], claimed, platform_buckets)
        report.counts["orphan_buckets"] = len(report.orphan_buckets)

    if (reason := _first(sources.bindings_error, sources.warehouse_records_error)) is not None:
        report.unavailable.append(CategoryUnavailable(category="dangling_bindings", reason=reason))
    else:
        report.dangling_bindings = _dangling_bindings(sources.bindings or [], warehouse_ids)
        report.counts["dangling_bindings"] = len(report.dangling_bindings)

    if (reason := _first(sources.tuples_error, sources.project_records_error)) is not None:
        report.unavailable.append(CategoryUnavailable(category="orphaned_annotation_tasks", reason=reason))
    else:
        report.orphaned_annotation_tasks = _orphaned_annotation_tasks(sources.tuples.annotation_tenants if sources.tuples else [], project_ids)
        report.counts["orphaned_annotation_tasks"] = len(report.orphaned_annotation_tasks)

    # `total` is the PURGE GATE, not the drift count — every category stays in `counts` and is still
    # reported. See `NON_GATING_CATEGORIES` for why these two are reported without gating.
    report.total = sum(count for name, count in report.counts.items() if name not in NON_GATING_CATEGORIES)
    return report


_ORPHAN_SCAN_OFF = "MAINTENANCE_ORPHAN_SCAN_ENABLED is off — the per-dataset file scan is a different order of work from the store comparison"


def _scannable_buckets(report: ReconcileReport, settings: MaintenanceSettings, sources: Sources) -> list[str]:
    """Every bucket the orphan scan should open: the configured list PLUS every registered warehouse.

    #81's residual, closed here. `run_sweep` learned to read the warehouse REGISTRY — a bucket is
    created by an API CALL at runtime, so every tenant provisioned since the last config edit was
    invisible to a static list — but this scan kept iterating `sweep_buckets` alone. Same blindness,
    worse consequence: the sweep merely failed to MAINTAIN those datasets, while this reports zero
    orphans for them, and a zero in a drift report is read as "checked and clean".

    Reuses the registry `_read_sources` ALREADY loaded rather than reading it again. Two reads would
    be two answers — the second could disagree with the one `orphan_buckets` and `dangling_bindings`
    were computed from, and a report whose categories disagree about which warehouses exist is worse
    than one that is merely incomplete.

    The FAILURE posture differs from the sweep's deliberately. The sweep logs a warning and degrades
    to the configured list, which is exactly its old behaviour and costs only a maintenance cycle. A
    REPORT may not do that: an unreadable registry means buckets nobody looked at, so it is recorded
    as an IncompleteScan. "A 0 that means 'we did not look' is the one number this report must never
    print" is this section's own rule, and a silent degrade is precisely how that 0 gets printed.
    """
    buckets = list(settings.sweep_buckets)
    if sources.warehouse_records_error is not None:
        report.incomplete.append(
            IncompleteScan(
                source="registry:warehouses",
                reason=f"warehouse registry unreadable, so any bucket outside the configured list went UNSCANNED: {sources.warehouse_records_error}",
            )
        )
        return buckets
    for record in sources.warehouse_records or []:
        bucket = str(record.get("bucket") or "")
        # Deactivated warehouses are still SCANNED, unlike the sweep, which skips them. Reporting is
        # not rewriting: a quarantined tenant's orphans are exactly what an operator wants named
        # before deciding whether to offboard it, and naming them changes nothing on disk.
        if bucket and bucket not in buckets:
            buckets.append(bucket)
    return buckets


def _orphan_category(report: ReconcileReport, settings: MaintenanceSettings, sources: Sources) -> None:
    """Attach the unreferenced-file findings, or say honestly why they are absent.

    Gated separately because it opens EVERY dataset and unions the file references of EVERY live
    version, across the data buckets — a different order of work from comparing three stores. When
    off it is SKIPPED with a reason, never reported as zero: a 0 that means "we did not look" is the
    one number this report must never print.
    """
    if not settings.orphan_scan_enabled:
        # coverage_gap: the rule APPLIES and we did not run it, so the file layer is unexamined. That
        # blocks `report_is_clean`, and therefore the #79 purge — reclaiming bytes on the strength of
        # a scan that never opened a dataset is the failure the report-first rule exists to prevent.
        report.skipped.append(CategorySkipped(category="orphan_files", reason=_ORPHAN_SCAN_OFF, coverage_gap=True))
        return
    fs, _ = fs_and_base(f"s3://{settings.s3_bucket}", settings.storage_options())
    datasets: list[tuple[str, str]] = []
    for bucket in _scannable_buckets(report, settings, sources):
        try:
            found = discover_datasets(fs, bucket)
        except Exception as exc:
            report.incomplete.append(IncompleteScan(source=f"storage:{bucket}", reason=f"dataset discovery failed: {exc}"))
            continue
        datasets.extend((uri, uri.removeprefix("s3://")) for uri in found.uris)
        # The depth bound is a REAL gap in coverage, so it becomes a real incomplete-scan note: a
        # dataset nested deeper than the walk reaches was never opened, and `report_is_clean` — the
        # gate the #79 purge waits on — must not certify an estate whose file layer we only partly saw.
        for prefix in found.truncated:
            report.incomplete.append(IncompleteScan(source=f"storage:{bucket}", reason=f"depth limit reached at {prefix} — datasets under it were not scanned"))
    scan = scan_datasets(fs, datasets, settings.storage_options())
    report.orphan_files = scan.orphans
    report.counts["orphan_files"] = len(scan.orphans)
    report.total += len(scan.orphans)
    for note in scan.incomplete:
        report.incomplete.append(IncompleteScan(source="storage:datasets", reason=note))


async def reconcile(
    settings: MaintenanceSettings,
    client: Any = None,
    *,
    warehouses_enabled: bool,
    control_root: str | None = None,
    namespace_root: str | None = None,
    platform_buckets: Iterable[str] | None = None,
    fga_root_object: str = DEFAULT_FGA_ROOT_OBJECT,
    bucket_client: Any = None,
) -> ReconcileReport:
    """Scan the three stores and REPORT their disagreements. Deletes nothing; mutates nothing.

    ``client`` is an OpenFGA client (``None`` → the two ghost categories and ``unreferenced_projects``
    report unavailable rather than silently empty). ``bucket_client`` is an S3 client (``None`` → same,
    for ``orphan_buckets``); when omitted one is built from the sweep's own credentials.

    ``control_root`` defaults to the root the sweep already reads the maintenance-policy registry from
    (``MAINTENANCE_POLICY_ROOT``, else the primary bucket) — the catalog's control root, and the one
    place the project/warehouse/binding records live. ``namespace_root`` defaults to the primary
    lakehouse bucket: the SHARED default root, where a namespace with no warehouse binding lands.

    ``warehouses_enabled`` mirrors the catalog's own switch and is passed IN — compaction does not own
    that setting, and inferring it from the presence of records would report an estate mid-migration as
    clean. With warehouses off, ``unbound_namespaces`` is skipped, not zero.
    """
    # `resolved_control_root`, not `resolved_policy_root`. This defaulted to the POLICY root while its
    # own docstring above says "the catalog's control root … the one place the project/warehouse/binding
    # records live" — two different settings that happen to coincide whenever neither is overridden.
    # Latent only because `routes.py` passes the value explicitly; every other caller, and every test,
    # read the wrong default, so an estate that moved its policy root would have had the drift report
    # look for project and warehouse records where the policies live and find none — reporting every
    # tenant as a ghost.
    resolved_control_root = control_root or settings.resolved_control_root
    resolved_namespace_root = namespace_root or f"s3://{settings.s3_bucket}"
    storage_options = settings.storage_options()
    if bucket_client is None and resolved_namespace_root.startswith("s3://"):
        # Via the canonical wrapper (packages/storage), never boto3 directly, and with the sweep's own
        # credentials passed explicitly — the env chain is one global pair and this process may address
        # a different backend than the ambient one.
        from storage import s3_client

        bucket_client = s3_client(
            settings.s3_endpoint,
            access_key=settings.s3_access_key_id,
            secret_key=settings.s3_secret_access_key.get_secret_value(),
        )

    # `settings.platform_buckets`, not `sweep_buckets`: the swept set alone omits infrastructure the
    # estate creates for ITSELF and never governs — `rask-observability` (GreptimeDB's object store,
    # created by the chart's own mkbucket job) was reported as an orphan bucket on every tick, which is a
    # finding no operator can ever clear and which therefore held the drift total permanently above zero.
    platform = {b for b in (platform_buckets if platform_buckets is not None else settings.platform_buckets) if b}
    for uri in (resolved_control_root, resolved_namespace_root):
        if (bucket := _bucket_of(uri)) is not None:
            platform.add(bucket)

    sources = await load_sources(
        client=client,
        control_root=resolved_control_root,
        namespace_root=resolved_namespace_root,
        storage_options=storage_options,
        bucket_client=bucket_client,
        delimiter=settings.delimiter,
    )
    report = build_report(
        sources,
        warehouses_enabled=warehouses_enabled,
        platform_buckets=platform,
        fga_root_object=fga_root_object,
    )
    # The unreferenced-FILE pass runs after the store comparison and is separately gated: it opens
    # every dataset rather than comparing three stores, so it is a different order of work. Blocking
    # Lance/S3 IO, so it goes to the threadpool like every other read here.
    await run_in_threadpool(_orphan_category, report, settings, sources)
    log.info(
        "reconcile_report",
        extra={
            "total": report.total,
            "counts": report.counts,
            "unavailable": [u.category for u in report.unavailable],
            "incomplete": len(report.incomplete),
        },
    )
    return report
