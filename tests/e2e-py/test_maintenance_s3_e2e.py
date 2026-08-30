"""#80 — the maintenance sweep and the orphan scan, against REAL object storage.

Every orphan assertion in the estate was written against ``pafs.LocalFileSystem`` (tests/unit/
test_orphan_files.py). That is the right place to pin the RULES, and it cannot pin the STORE: the
three things this pass depends on are all things a local filesystem gets right for free and an object
store does differently.

1. **Directories are synthetic.** ``_unscannable_reason`` refuses a branched dataset by asking
   ``get_file_info(f"{prefix}/tree").type == Directory``. On S3 there is no such thing as a
   directory — ``tree/`` exists only as a shared key prefix, and whether the store SYNTHESISES a
   ``Directory`` FileInfo for it is a property of the store, not of Lance. If it answers ``NotFound``
   the branch gate silently opens and every file of every branch is reported as garbage.
2. **Paths are ``bucket/key``, not filesystem paths.** ``scan_dataset`` derives its relative path with
   ``posixpath.relpath(info.path, prefix)`` where ``prefix`` is ``bucket/table`` — a shape the local
   tests never produce, and the shape every refusal comparison (``rel == ".lance-reserved"``,
   ``rel.startswith("_refs/")``, the sidecar prefix test) is applied to.
3. **The blob sidecar is a real Lance write, not a fabricated directory.** The local test MAKES the
   ``data/<stem>/*.blob`` tree by hand. Here Lance writes it, from a ``blob_field``/``blob_array``
   column at ``data_storage_version="2.2"`` — so the sidecar under test is the layout Lance actually
   produces on S3, which is what the 2026-08-04 live run got wrong (29 MB of real page images named
   reclaimable).

And the sweep half: #81 taught ``run_sweep`` to union its configured buckets with the warehouse
REGISTRY, and ``reconcile._scannable_buckets`` to do the same but WITHOUT excluding deactivated
warehouses — reporting is not rewriting. That asymmetry has only ever had fake-filesystem coverage.

Order matters and is enforced by the fixture graph: the sweep runs FIRST and the scan runs over what
the sweep left behind, because that is the production order (both are cron-driven against the same
buckets) and because a scan that only passes on datasets nobody compacted proves nothing about the
estate. Compaction leaves the pre-compaction data files referenced by OLDER live versions, so a scan
that mistook "referenced by the latest version" for "referenced" would fail exactly here.

**Self-contained and destructive-by-nature.** ``run_sweep`` compacts and GCs every dataset it
discovers, so this suite never points at an existing bucket: it creates four uuid-suffixed buckets of
its own, sweeps only those, and deletes them. ``LANCE_E2E_S3_BUCKET`` is deliberately NOT read —
that variable names the shared catalog bucket the CAS suite uses.

**Scope, stated plainly.** The warehouse registry records are written as JSON directly to the control
root rather than through ``POST /v1/warehouses``. That contradicts seed_estate.py's "drive the real
doors" rule and is a deliberate bound: what is under test is the READER
(``warehouse_records.list_warehouse_records``) and the two bucket-set unions built on it, not catalog
provisioning — which ``test_warehouses_e2e.py`` already drives, and which would re-gate this suite
behind a deployed catalog and two tokens. This suite proves nothing about warehouse creation.

Env-gated on ``LANCE_E2E_S3_ENDPOINT`` (the convention ``test_object_store_cas_e2e.py`` established
and ``scripts/e2e_stack.sh`` exports). Run: ``make e2e-stack``, or point it at any S3 store:

    LANCE_E2E_S3_ENDPOINT=http://localhost:9900 uv run pytest tests/e2e-py/test_maintenance_s3_e2e.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import lance
import pyarrow as pa
import pyarrow.fs as pafs
import pytest
from lance import blob_array, blob_field
from pydantic import BaseModel

from maintenance.core.config import MaintenanceSettings
from maintenance.services import orphans
from maintenance.services.optimize import DatasetResult
from maintenance.services.reconcile import ReconcileReport, reconcile
from maintenance.services.sweep import run_sweep, summarize
from service_kit.lakehouse.objectfs import s3_filesystem


ENDPOINT = os.environ.get("LANCE_E2E_S3_ENDPOINT", "")
ACCESS_KEY = os.environ.get("LANCE_E2E_S3_ACCESS_KEY", "rustfsadmin")
SECRET_KEY = os.environ.get("LANCE_E2E_S3_SECRET_KEY", "rustfsadmin")

#: Reuses the REGISTERED `compaction` marker (pyproject `markers`) — `make e2e-compaction` selects it.
pytestmark = [pytest.mark.e2e, pytest.mark.compaction]

#: Stray `data/*.lance` files planted by hand — the POSITIVE controls. Without them every "is not
#: reported" assertion below would also pass on a scan that reports nothing at all.
STRAY_IN_SWEPT = "data/00000000000000000000000000deadbeef.lance"
STRAY_IN_DEACTIVATED = "data/000000000000000000000000cafebabe00.lance"


class Estate(BaseModel):
    """The four throwaway buckets and the six dataset URIs written across them."""

    ctl: str
    extra: str
    reg: str
    deact: str
    plain_uri: str
    blob_uri: str
    branch_uri: str
    extra_uri: str
    reg_uri: str
    deact_uri: str

    @property
    def swept_uris(self) -> set[str]:
        """Every dataset the sweep must find — the deactivated warehouse's is deliberately absent."""
        return {self.plain_uri, self.blob_uri, self.branch_uri, self.extra_uri, self.reg_uri}


def _rows(n: int = 3, offset: int = 0) -> pa.Table:
    return pa.table({"id": pa.array(range(offset, offset + n), pa.int64()), "v": pa.array([f"r{i}" for i in range(n)])})


def _wipe(client: Any, bucket: str) -> None:
    """Delete every object in ``bucket`` then the bucket itself. Best-effort — teardown must not red a run."""
    try:
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket)
        for page in pages:
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                client.delete_objects(Bucket=bucket, Delete={"Objects": keys})
        client.delete_bucket(Bucket=bucket)
    except Exception as exc:  # noqa: BLE001 — a leaked test bucket is untidy; a failed teardown is misleading
        print(f"teardown: could not remove {bucket}: {exc}")  # noqa: T201


@pytest.fixture(scope="module")
def s3() -> Any:
    """A client on the real store, via packages/storage's wrapper — never raw boto3 (repo rule)."""
    if not ENDPOINT:
        pytest.skip("set LANCE_E2E_S3_ENDPOINT (see the module docstring / scripts/e2e_stack.sh)")
    from storage import s3_client

    client = s3_client(ENDPOINT, access_key=ACCESS_KEY, secret_key=SECRET_KEY)
    try:
        client.list_buckets()
    except Exception as exc:  # noqa: BLE001 — no store, no suite; the reason belongs in the skip
        pytest.skip(f"S3 store unreachable at {ENDPOINT}: {exc}")
    return client


@pytest.fixture(scope="module")
def estate(s3: Any) -> Iterator[Estate]:
    """Four fresh buckets carrying six real Lance datasets, plus two warehouse registry records.

    Bucket names are lowercase/hyphen/uuid — the S3 legal set, which excludes the ``$`` and ``_`` a
    catalog table id carries, so nothing here may be derived from one.
    """
    tag = uuid.uuid4().hex[:8]
    names = {k: f"rask-e2e-maint-{tag}-{k}" for k in ("ctl", "extra", "reg", "deact")}
    try:
        for bucket in names.values():
            s3.create_bucket(Bucket=bucket)
    except Exception as exc:  # noqa: BLE001 — a scoped/vended credential cannot create buckets
        for bucket in names.values():
            _wipe(s3, bucket)
        pytest.skip(f"the credential at {ENDPOINT} may not create buckets, which this suite requires: {exc}")

    built = Estate(
        **names,
        plain_uri=f"s3://{names['ctl']}/plain",
        blob_uri=f"s3://{names['ctl']}/blobtbl",
        branch_uri=f"s3://{names['ctl']}/branched",
        extra_uri=f"s3://{names['extra']}/extratbl",
        reg_uri=f"s3://{names['reg']}/regtbl",
        deact_uri=f"s3://{names['deact']}/deacttbl",
    )
    options = _settings_for(built).storage_options()

    # THREE separate appends, so compaction has real work to do on object storage. A single-fragment
    # dataset would make the sweep a no-op and every assertion below vacuous.
    lance.write_dataset(_rows(), built.plain_uri, storage_options=options)
    lance.write_dataset(_rows(offset=10), built.plain_uri, mode="append", storage_options=options)
    lance.write_dataset(_rows(offset=20), built.plain_uri, mode="append", storage_options=options)
    # A TAG — `_refs/tags/blessed.json`, a live version pointer. The first live run of this pass named
    # every promotion tag in the estate as an orphan.
    lance.dataset(built.plain_uri, storage_options=options).tags.create("blessed", 1)

    # A REAL blob sidecar: blob-v2 at data_storage_version 2.2 writes the bytes to
    # `data/<data-file-stem>/*.blob`, which `data_files()` does not name. Payloads are ~120 KB — big
    # enough that Lance sidecars them, small enough that the suite stays fast.
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("payload")])
    lance.write_dataset(
        pa.table({"id": [1, 2], "payload": blob_array([b"x" * 120_000, b"y" * 120_000])}, schema=schema),
        built.blob_uri,
        mode="overwrite",
        data_storage_version="2.2",
        storage_options=options,
    )

    # A BRANCH — a whole parallel dataset under `tree/feature-a/`, invisible to the main branch's manifest.
    branch = lance.write_dataset(_rows(), built.branch_uri, storage_options=options).create_branch("feature-a")
    lance.write_dataset(_rows(2), branch, mode="append", storage_options=options)

    for uri in (built.extra_uri, built.reg_uri, built.deact_uri):
        lance.write_dataset(_rows(), uri, storage_options=options)

    # The warehouse REGISTRY, on the control root — the record shape `catalog/services/warehouses.py`
    # writes and `warehouse_records.list_warehouse_records` reads. `reg` is active, `deact` quarantined.
    for wid, bucket, status in (("wh-reg", built.reg, "active"), ("wh-deact", built.deact, "deactivated")):
        s3.put_object(
            Bucket=built.ctl,
            Key=f"_warehouses/{wid}.json",
            Body=json.dumps({"id": wid, "project": "e2e-maint", "bucket": bucket, "status": status}).encode(),
        )

    yield built

    for bucket in names.values():
        _wipe(s3, bucket)


def _settings_for(estate: Estate) -> MaintenanceSettings:
    """Settings pinned to THIS estate. Never `get_settings()` — that is lru_cached off the ambient env,
    and the one thing this suite must never do is sweep a bucket it did not create."""
    return MaintenanceSettings(
        s3_endpoint=ENDPOINT,
        s3_access_key_id=ACCESS_KEY,
        s3_secret_access_key=SECRET_KEY,
        s3_bucket=estate.ctl,
        s3_extra_buckets=estate.extra,
        s3_region="us-east-1",
        older_than_days=1,
        orphan_scan_enabled=True,
        fga_enabled=False,
        warehouses_enabled=True,
    )


@pytest.fixture(scope="module")
def settings(estate: Estate) -> MaintenanceSettings:
    return _settings_for(estate)


@pytest.fixture(scope="module")
def swept(settings: MaintenanceSettings) -> list[DatasetResult]:
    """ONE real sweep over the estate. Module-scoped: it rewrites data files, so running it per test
    would both cost minutes and mean each test saw a different dataset layout."""
    return run_sweep(settings)


@pytest.fixture(scope="module")
def scanned(estate: Estate, settings: MaintenanceSettings, s3: Any, swept: list[DatasetResult]) -> ReconcileReport:
    """The orphan scan, run AFTER the sweep (production order — the `swept` dependency is what
    guarantees it) over datasets a compaction has just rewritten.

    Two stray `data/*.lance` files are planted first: one under a swept dataset and one under the
    DEACTIVATED warehouse's. They are the positive controls — a scan that reports nothing would
    satisfy every refusal assertion in this file, and these are what make that impossible.

    `.lance-reserved` is planted too, and unlike the tag and the blob sidecar it is NOT something
    Lance wrote here: pylance 9 does not lay one down for these datasets. Stated plainly rather than
    left implied, because an assertion about a file that does not exist is an assertion about
    nothing. What it does buy is real — the exclusion is an EXACT-match comparison
    (`rel == ".lance-reserved"`) against a path `posixpath.relpath` derived from `bucket/key`, and
    that arithmetic is the thing a LocalFileSystem test cannot reach.
    """
    assert swept, "the sweep must have run before the scan — the production order is the thing under test"
    s3.put_object(Bucket=estate.ctl, Key=f"plain/{STRAY_IN_SWEPT}", Body=b"a fragment no manifest references")
    s3.put_object(Bucket=estate.deact, Key=f"deacttbl/{STRAY_IN_DEACTIVATED}", Body=b"orphaned in a quarantined tenant")
    s3.put_object(Bucket=estate.ctl, Key="plain/.lance-reserved", Body=b"")
    # `client=None` → the FGA-backed categories report UNAVAILABLE, which is correct and irrelevant
    # here; nothing below reads them. control_root is THIS estate's, so no real registry is touched.
    return asyncio.run(reconcile(settings, None, warehouses_enabled=True, control_root=f"s3://{estate.ctl}"))


# --------------------------------------------------------------------------- #
# The sweep, on real object storage
# --------------------------------------------------------------------------- #


def test_the_sweep_names_exactly_the_datasets_it_found_on_s3(estate: Estate, swept: list[DatasetResult]) -> None:
    """Discovery is right in BOTH directions: every dataset found, and nothing invented.

    Set equality rather than `>= 1` (which is all `test_maintenance_e2e.py` can assert against a
    deployed stack whose contents it does not control): a sweep that missed a bucket and a sweep that
    reported a namespace prefix as a failed dataset both satisfy a floor.
    """
    assert {r.uri for r in swept} == estate.swept_uris
    assert summarize(swept)["errors"] == {}, "a dataset failed its maintenance pass on real object storage"


def test_the_sweep_actually_compacts_on_object_storage(estate: Estate, swept: list[DatasetResult]) -> None:
    """The anti-vacuous control. Without it every other assertion here passes on a sweep that opened
    five datasets and did nothing — which is precisely what a silently mis-credentialled or
    mis-pathed sweep looks like from the outside.

    `fragments_removed`, not `old_versions_removed`: `older_than_days` is `ge=1`, so no version of a
    dataset written seconds ago is ever eligible for reclamation.
    """
    plain = next(r for r in swept if r.uri == estate.plain_uri)
    assert plain.fragments_removed >= 1 and plain.fragments_added >= 1, (
        f"three appends should have been compacted into fewer fragments, got {plain.model_dump()}"
    )


def test_a_registry_only_bucket_is_swept_and_a_deactivated_one_is_not(estate: Estate, swept: list[DatasetResult]) -> None:
    """#81's residual, closed against real storage.

    `reg` is named by NO config — only by a warehouse record the sweep reads at runtime, which is the
    whole point: a bucket is created by an API call, so a config-time list goes stale by construction.
    `deact` is named by a record too, and must be skipped: deactivate is offboarding step one, and
    compacting a quarantined tenant would leave maintenance as the one process still rewriting data
    the estate has said nobody may touch.
    """
    found = {r.uri for r in swept}
    assert estate.reg_uri in found, "a bucket known only to the warehouse registry went unmaintained"
    assert estate.deact_uri not in found, "a DEACTIVATED warehouse's data was rewritten by the sweep"


# --------------------------------------------------------------------------- #
# The orphan scan — the refusal classes, on real S3 semantics for the first time
# --------------------------------------------------------------------------- #


def test_the_orphan_scan_ran_and_did_not_report_a_zero_it_never_measured(scanned: ReconcileReport) -> None:
    """The gate on every assertion below: the category must have RUN.

    With `MAINTENANCE_ORPHAN_SCAN_ENABLED` off, `_orphan_category` records a CategorySkipped and
    leaves `orphan_files` empty — and an empty list satisfies every "is not reported" test in this
    file. So pin the gate first: the count exists, and the category is not in `skipped`.
    """
    assert "orphan_files" in scanned.counts, "the orphan category never produced a count — it did not run"
    assert "orphan_files" not in {s.category for s in scanned.skipped}
    assert scanned.counts["orphan_files"] > 0, "the planted strays should have been found"


def test_a_stray_data_file_on_s3_is_still_reported(estate: Estate, scanned: ReconcileReport) -> None:
    """The positive control: a `data/*.lance` under a live dataset's prefix that no manifest names IS
    an orphan, and is classified as `data` — the kind whose reclamation loses rows, so the kind a
    reader must never learn to ignore."""
    found = {(o.dataset, o.path, o.kind) for o in scanned.orphan_files}
    assert (estate.plain_uri, STRAY_IN_SWEPT, "data") in found, f"the planted orphan was not reported: {sorted(found)}"


def test_a_real_blob_sidecar_written_by_lance_is_not_an_orphan(estate: Estate, settings: MaintenanceSettings, scanned: ReconcileReport) -> None:
    """The refusal that cost 29 MB of real page images on the first live run — now against bytes Lance
    itself wrote on S3, not a directory a test fabricated.

    The sidecar path is DERIVED from the referenced set rather than hardcoded: the stem is a Lance
    implementation detail, and a test that hardcoded it would keep passing if Lance changed the layout
    while the scan started naming live blobs.
    """
    referenced, _, note = orphans.referenced_paths(estate.blob_uri, settings.storage_options())
    assert note is None
    stems = [p.removeprefix("data/").removesuffix(".lance") for p in referenced if p.endswith(".lance")]
    assert stems, "the blob fixture must have at least one referenced data file"

    fs = s3_filesystem(settings.storage_options())
    sidecars = [
        info.path
        for info in fs.get_file_info(pafs.FileSelector(f"{estate.ctl}/blobtbl/data/{stems[0]}", allow_not_found=True, recursive=True))
        if info.path.endswith(".blob")
    ]
    assert sidecars, "the fixture must really have produced a `.blob` sidecar on S3 — otherwise this test asserts nothing"

    named = {(o.dataset, o.path) for o in scanned.orphan_files}
    for path in sidecars:
        relative = path.removeprefix(f"{estate.ctl}/blobtbl/")
        assert (estate.blob_uri, relative) not in named, f"a LIVE blob sidecar Lance wrote was named an orphan: {relative}"
    assert not any(o.path.endswith(".blob") for o in scanned.orphan_files), "some `.blob` was reported across the estate"


def test_a_tag_is_not_reported_as_an_orphan_on_s3(estate: Estate, settings: MaintenanceSettings, scanned: ReconcileReport) -> None:
    """`_refs/tags/*.json` PINS a version — `cleanup_old_versions` exempts tagged versions for that
    reason, so a reclaimer acting on a tag would unpin published data and then free the versions the
    tag was protecting.

    The exclusion is `rel.startswith("_refs/")` where `rel` came out of `posixpath.relpath` over a
    `bucket/key` path. That is the comparison this test exercises and the local suite cannot.
    """
    fs = s3_filesystem(settings.storage_options())
    tags = fs.get_file_info(pafs.FileSelector(f"{estate.ctl}/plain/_refs", allow_not_found=True, recursive=True))
    assert [t.path for t in tags if t.path.endswith(".json")], "the fixture must really have written a tag file on S3"
    assert not any(o.path.startswith("_refs/") for o in scanned.orphan_files), (
        f"a ref was reported as an orphan: {[o.path for o in scanned.orphan_files if o.path.startswith('_refs/')]}"
    )


def test_the_version_index_and_reserved_marker_are_not_reported(estate: Estate, settings: MaintenanceSettings, scanned: ReconcileReport) -> None:
    """A manifest is what MAKES a version live, so it can never be unreferenced while it is present,
    and `.lance-reserved` is a zero-byte structural marker no manifest ever names. Reporting either
    every run is how a report trains its reader to ignore it.

    Both halves are checked for PRESENCE first. `_versions/*.manifest` is Lance's own — five of them
    here, one per commit; `.lance-reserved` is planted by the `scanned` fixture (see its docstring).
    Without the presence checks this test would pass just as happily on an empty prefix.
    """
    fs = s3_filesystem(settings.storage_options())
    listed = {info.path for info in fs.get_file_info(pafs.FileSelector(f"{estate.ctl}/plain", allow_not_found=True, recursive=True))}
    assert any(p.endswith(".manifest") for p in listed), "the fixture must really have manifests on S3"
    assert f"{estate.ctl}/plain/.lance-reserved" in listed, "the reserved marker was not planted — this test would assert nothing"

    named = [o.path for o in scanned.orphan_files if o.path.startswith("_versions/") or o.path == ".lance-reserved"]
    assert named == [], f"the version index / reserved marker was reported: {named}"


def test_a_branch_is_refused_on_object_storage(estate: Estate, settings: MaintenanceSettings, scanned: ReconcileReport) -> None:
    """The one refusal whose MECHANISM is directory-shaped, and therefore genuinely different here.

    `_unscannable_reason` asks whether `<prefix>/tree` is a Directory. S3 has no directories — `tree/`
    is a shared key prefix, and a synthesised `Directory` FileInfo for it is a property of the store's
    listing behaviour. If the store answered `NotFound` the gate would open and every file of every
    branch would be reported as garbage (measured on the local suite: 6 of 7 findings, including the
    branch's own `data/*.lance`).
    """
    result = orphans.scan_dataset(
        s3_filesystem(settings.storage_options()),
        estate.branch_uri,
        prefix=f"{estate.ctl}/branched",
        storage_options=settings.storage_options(),
    )
    assert result.checked is False, "a branched dataset was SCANNED on S3 — the tree/ gate did not fire"
    assert result.orphans == []
    assert "tree/" in (result.reason or "")
    # ...and the aggregate report says so out loud rather than counting it as clean.
    assert any("tree/" in note.reason for note in scanned.incomplete), "the refusal never reached the report's incomplete list"
    assert not any(o.dataset == estate.branch_uri for o in scanned.orphan_files)


def test_a_deactivated_warehouse_is_still_scanned_even_though_it_is_not_swept(estate: Estate, scanned: ReconcileReport) -> None:
    """The deliberate asymmetry between `warehouse_records.maintainable_buckets` (excludes
    deactivated) and `reconcile._scannable_buckets` (includes them), pinned end to end on real
    storage.

    Reporting is not rewriting. A quarantined tenant's orphans are exactly what an operator wants
    named before deciding whether to offboard it, and naming them changes nothing on disk — while
    compacting the same bytes would not. `test_a_registry_only_bucket_is_swept_and_a_deactivated_one_is_not`
    proves the other half: this same dataset was NOT swept.
    """
    found = {(o.dataset, o.path) for o in scanned.orphan_files}
    assert (estate.deact_uri, STRAY_IN_DEACTIVATED) in found, (
        f"a deactivated warehouse's bucket went unscanned — its zero would read as 'checked and clean': {sorted(found)}"
    )
