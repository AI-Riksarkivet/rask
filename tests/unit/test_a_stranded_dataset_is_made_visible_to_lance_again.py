"""Raising the listing floor makes Lance reclaim residue it had gone permanently blind to ([[LH-094]]).

Owner ruling 2026-09-19 (`docs/adr/0066-four-rulings-on-the-lakehouse-s-unblocked-rows-owner-2026.md`): rask does not delete the stranded bytes. It writes one
metadata-only commit, which moves the dataset's floor above them, and Lance's own
`cleanup_old_versions` does the deleting under its own rule on the next ordinary sweep.

THE FIRST TEST DRIVES REAL LANCE, not a fake, because the claim being made is about upstream's
behaviour and a fake would only restate the belief under test. It builds the estate's measured shape —
a dataset collapsed to one live version with an unreferenced transaction file whose mtime is ABOVE the surviving
manifest's commit timestamp but in the past — and asserts the before/after: `cleanup_old_versions`
removes nothing, then after one config commit it removes the file and the rows still read.

The remaining tests drive the selection, which is where the judgement lives: which datasets are
eligible, which are refused, and what a dry run does.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import lance
import pyarrow as pa
import pytest
import respx

from lakehouse import trash
from lakehouse.base_refs import normalise
from lakehouse.lancekit.versions import committed_at
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.services.credentials import MaintenanceDenied
from maintenance.services.due import TableIds
from maintenance.services.floor import FLOOR_KEY, FloorReport, raise_listing_floors
from maintenance.services.orphans import OrphanFile


CATALOG = "http://catalog.test"

type Door = Callable[[list[OrphanFile]], TableIds]


def _settings(control_root: Path, *, enabled: bool = True, dry_run: bool = False, max_per_tick: int = 10, catalog_url: str = CATALOG) -> MaintenanceSettings:
    """The pass's settings, built for real, with the trash index under ``control_root`` and the catalog's vending door at
    ``catalog_url``, answered through respx (`door`): a floor commit signs with its table's vend and nothing else ([[LH-219]])."""
    return MaintenanceSettings(
        control_root=str(control_root),
        floor_raise_enabled=enabled,
        floor_raise_dry_run=dry_run,
        floor_raise_max_per_tick=max_per_tick,
        catalog_url=catalog_url,
    )


def _stranded_dataset(root: Path) -> tuple[str, Path, float]:
    """A live dataset at one version, holding one unreferenced file above its own listing floor.

    THE ORPHAN'S MTIME IS IN THE PAST, and that is the fixture's whole difficulty. A file planted in
    the FUTURE is above the floor and stays above it after the commit too, so the mechanism appears not
    to work — which is exactly the wrong reading, and the first probe of this made it. The estate's real
    shape is objects re-uploaded a week AFTER the commits that reference them and a week BEFORE now.
    """
    uri = str(root / "ds")
    dataset = lance.write_dataset(pa.table({"i": pa.array([1, 2, 3])}), uri)
    dataset = lance.write_dataset(pa.table({"i": pa.array([4])}), uri, mode="append")
    dataset.cleanup_old_versions(older_than=dt.timedelta(seconds=0), delete_unverified=True)
    dataset = lance.dataset(uri)
    floor = min(committed_at(version).timestamp() for version in dataset.versions())

    # A TRANSACTION file, because Lance lists `_transactions/` and `_deletions/` only up to the floor; a
    # `data/` file is listed past it (lance #8944) and so is never stranded.
    orphan = Path(uri) / "_transactions" / "9-dead.txn"
    orphan.write_bytes(b"x" * 64)
    planted = floor + 0.5
    os.utime(orphan, (planted, planted))
    # So the planted mtime is in the past by the time cleanup runs; otherwise the raised floor lands
    # under it and the mechanism is measured against a file that has not happened yet.
    time.sleep(1.5)
    return uri, orphan, floor


def test_lance_reclaims_a_stranded_file_only_after_the_floor_is_raised(door: Door, tmp_path: Path) -> None:
    """The mechanism, end to end, against pylance itself."""
    uri, orphan, floor = _stranded_dataset(tmp_path)
    assert orphan.stat().st_mtime > floor, "the fixture did not strand the file above the floor"

    lance.dataset(uri).cleanup_old_versions(older_than=dt.timedelta(seconds=0), delete_unverified=True)
    assert orphan.exists(), "Lance saw a file the floor should have hidden — the fixture is wrong, not Lance"

    orphans = [
        OrphanFile(
            dataset=uri, path="_transactions/9-dead.txn", kind="transactions", size_bytes=64, reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime
        )
    ]
    report = raise_listing_floors(_settings(tmp_path), orphans=orphans, names=door(orphans), storage_options=StorageOptions())
    assert [r.dataset for r in report.raised] == [uri], report

    lance.dataset(uri).cleanup_old_versions(older_than=dt.timedelta(seconds=0), delete_unverified=True)
    assert not orphan.exists()


def _named(orphans: list[OrphanFile]) -> TableIds:
    """The tick's ``__manifest`` read, naming every orphan's dataset a table of its own."""
    return TableIds(by_location={normalise(orphan.dataset): f"db${index}" for index, orphan in enumerate(orphans)})


def _vend_covering(location: str) -> httpx.Response:
    """The door's maintain-tier vend for the table at ``location``: a scoped pair, and the table's own location."""
    vended = {"aws_access_key_id": "VENDED", "aws_secret_access_key": "vended-secret"}
    return httpx.Response(200, json={"mode": "direct", "credentials": {"storage_options": vended}, "location": location})


@pytest.fixture
def door(monkeypatch: pytest.MonkeyPatch) -> Iterator[Door]:
    """The catalog's vending door: `door(orphans)` names each orphan's dataset a table of its own (`_named`) and answers
    that table's maintain tier with a credential covering the dataset. A test takes it only when its pass vends."""
    monkeypatch.setattr("maintenance.services.credentials.service_headers", lambda _settings: {})
    locations: dict[str, str] = {}
    with respx.mock() as router:
        router.post(url__regex=rf"^{CATALOG}/management/v1/table/(?P<table>[^/]+)/credentials", params={"tier": "maintain"}).mock(
            side_effect=lambda _request, table: _vend_covering(locations[table])
        )

        def _serve(orphans: list[OrphanFile]) -> TableIds:
            names = _named(orphans)
            locations.update({names.by_location[normalise(orphan.dataset)]: orphan.dataset for orphan in orphans})
            return names

        yield _serve


def _orphan(dataset: str, reclaimable: bool | None, mtime: float = 2_000_000_000.0) -> OrphanFile:
    return OrphanFile(dataset=dataset, path="_transactions/x.txn", kind="transactions", reclaimable_by_lance=reclaimable, mtime_epoch=mtime)


@pytest.fixture
def readable_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A floor every fixture orphan sits above, so selection is exercised without a dataset per URI.

    STUBBED RATHER THAN SKIPPED, because a dry run deliberately performs the REAL floor re-read — the
    same rule the trash purge states, that a preview runs the real pass and skips only the mutations.
    Left unstubbed, every fake URI here would be refused `floor unreadable` and the selection these
    tests exist to drive would never be reached.
    """
    monkeypatch.setattr("maintenance.services.floor._current_floor", lambda uri, storage_options: 1_000_000_000.0)


def _plan(control_root: Path, orphans: list[OrphanFile], names: TableIds, **kwargs: Any) -> FloorReport:
    return raise_listing_floors(_settings(control_root, dry_run=True, **kwargs), orphans=orphans, names=names, storage_options=StorageOptions())


def test_a_disabled_pass_selects_nothing(readable_floor: None, tmp_path: Path) -> None:
    orphans = [_orphan("s3://wh/a", False)]
    report = raise_listing_floors(_settings(tmp_path, enabled=False), orphans=orphans, names=_named(orphans), storage_options=StorageOptions())

    assert report.enabled is False
    assert report.raised == [] and report.refused == []


def test_one_undecided_sibling_disqualifies_the_whole_dataset(readable_floor: None, tmp_path: Path) -> None:
    """All-or-nothing per dataset, because the commit is per dataset: raising the floor for one stranded
    file sweeps up its siblings whatever their class."""
    orphans = [_orphan("s3://wh/a", False), _orphan("s3://wh/a", None)]
    assert _plan(tmp_path, orphans, _named(orphans)).raised == []


def test_a_dataset_with_stranded_orphans_is_selected_beside_ones_time_clears(door: Door, readable_floor: None, tmp_path: Path) -> None:
    """The control, in the estate's shape: a restore strands transaction files beside data files, and
    the data files are Lance's whatever the floor (lance #8944). They neither earn the commit nor block
    it, and the raise names only the stranded files it is for."""
    data_file = OrphanFile(dataset="s3://wh/a", path="data/x.lance", kind="data", reclaimable_by_lance=True, mtime_epoch=2_000_000_000.0)
    orphans = [_orphan("s3://wh/a", False), _orphan("s3://wh/a", False), data_file, _orphan("s3://wh/b", True)]
    plan = _plan(tmp_path, orphans, door(orphans))

    assert [(r.dataset, r.orphan_files) for r in plan.raised] == [("s3://wh/a", 2)]


def test_a_dry_run_writes_no_version(door: Door, tmp_path: Path) -> None:
    uri, orphan, _ = _stranded_dataset(tmp_path)
    before = lance.dataset(uri).version

    orphans = [OrphanFile(dataset=uri, path="_transactions/9-dead.txn", kind="transactions", reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime)]
    report = raise_listing_floors(_settings(tmp_path, dry_run=True), orphans=orphans, names=door(orphans), storage_options=StorageOptions())

    assert [r.dataset for r in report.raised] == [uri], "a dry run must still report the plan"
    assert report.raised[0].version is None
    assert lance.dataset(uri).version == before
    assert orphan.exists()


def test_a_floor_that_already_covers_its_orphans_is_refused(tmp_path: Path) -> None:
    """Acting on a stale report. Something committed between the scan and now, so the next sweep will
    reclaim unaided and a commit here buys only the phantom version."""
    uri, _, _ = _stranded_dataset(tmp_path)
    lance.dataset(uri).update_config({"unrelated": "commit"})

    orphans = [OrphanFile(dataset=uri, path="_transactions/9-dead.txn", kind="transactions", reclaimable_by_lance=False, mtime_epoch=1.0)]
    report = raise_listing_floors(_settings(tmp_path), orphans=orphans, names=_named(orphans), storage_options=StorageOptions())

    assert report.raised == []
    assert [r.refused for r in report.refused] == ["floor already covers these orphans"]


def test_an_orphan_listed_in_the_floor_s_own_second_is_raised_not_refused(door: Door, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A whole-second listing cannot place a file inside the floor's second, so the scan strands it and the raise
    must not call it covered: the pass and the scan ask one rule (`orphans.listed_at_or_below`), or the file is
    stranded and refused on every tick."""
    monkeypatch.setattr("maintenance.services.floor._current_floor", lambda uri, storage_options: 1_000_000_000.559)

    orphans = [_orphan("s3://wh/a", False, mtime=1_000_000_000.0)]
    plan = _plan(tmp_path, orphans, door(orphans))

    assert ([r.dataset for r in plan.raised], plan.refused) == (["s3://wh/a"], [])


def test_an_unreadable_dataset_is_refused_and_does_not_stop_the_others(door: Door, tmp_path: Path) -> None:
    uri, orphan, _ = _stranded_dataset(tmp_path)

    orphans = [
        _orphan("s3://wh/does-not-exist", False),
        OrphanFile(dataset=uri, path="_transactions/9-dead.txn", kind="transactions", reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime),
    ]
    report = raise_listing_floors(_settings(tmp_path), orphans=orphans, names=door(orphans), storage_options=StorageOptions())

    assert [r.dataset for r in report.raised] == [uri]
    assert [r.refused for r in report.refused] == ["floor unreadable"]


def test_the_remainder_beyond_the_cap_is_reported(door: Door, readable_floor: None, tmp_path: Path) -> None:
    """A store rebuild strands EVERY dataset at once, so a truncated pass that said nothing would look
    complete and the next tick's smaller number would read as progress nobody made."""
    orphans = [_orphan(f"s3://wh/{i}", False) for i in range(5)]

    plan = _plan(tmp_path, orphans, door(orphans), max_per_tick=2)

    assert len(plan.raised) == 2
    assert plan.capped == 3


# --- the two defects the live run found -------------------------------------------------------- #


def test_the_commit_is_signed_by_the_vended_credential(door: Door, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A floor commit is a write to the table, so it signs with that table's vend and nothing else ([[LH-219]]).

    The pass's own options reach the reads that judge the dataset, never the commit. Found by deploying: the first
    version of this signed the commit with the options it was handed while the sweep, on the very same dataset, refused
    for want of a vended credential.
    """
    uri, orphan, _ = _stranded_dataset(tmp_path)
    opened: list[dict[str, str]] = []
    real = lance.dataset

    def _record(dataset_uri: str, *args: Any, **kwargs: Any) -> Any:
        opened.append(kwargs.get("storage_options") or {})
        return real(dataset_uri, *args, **{**kwargs, "storage_options": {}})

    monkeypatch.setattr(lance, "dataset", _record)
    own = StorageOptions.from_flat({"aws_access_key_id": "OWN", "aws_secret_access_key": "own-secret"})

    orphans = [OrphanFile(dataset=uri, path="_transactions/9-dead.txn", kind="transactions", reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime)]
    report = raise_listing_floors(_settings(tmp_path), orphans=orphans, names=door(orphans), storage_options=own)

    assert ([r.dataset for r in report.raised], "VENDED" in {options.get("aws_access_key_id") for options in opened}) == ([uri], True), opened


def test_a_catalog_denial_refuses_the_raise(monkeypatch: pytest.MonkeyPatch, readable_floor: None, tmp_path: Path) -> None:
    """A 403 means this identity may not write this table. Committing anyway is the bypass."""

    def _denied(*args: Any, **kwargs: Any) -> dict[str, str]:
        raise MaintenanceDenied("the catalog REFUSED a write credential for t (403)")

    monkeypatch.setattr("maintenance.services.floor.write_options_for", _denied)

    orphans = [_orphan("s3://wh/denied", False)]
    report = raise_listing_floors(_settings(tmp_path), orphans=orphans, names=_named(orphans), storage_options=StorageOptions())

    assert report.raised == []
    assert "REFUSED a write credential" in (report.refused[0].refused or "")


def test_a_table_the_catalog_does_not_govern_refuses_the_raise(monkeypatch: pytest.MonkeyPatch, readable_floor: None, tmp_path: Path) -> None:
    """A dataset no ``__manifest`` row names is refused unvended ([[LH-520]]), and one the vend door answers 404 for is
    refused too. Committing either under the ambient key is the same bypass."""
    monkeypatch.setattr("maintenance.services.credentials.service_headers", lambda _settings: {})
    settings = _settings(tmp_path, dry_run=True, catalog_url="http://catalog.test")
    named, unnamed = _orphan("s3://wh/0d9c6f3e-7b2a-4c1e-9f5d-2a8b3c4d5e6f", False), _orphan("s3://wh/5e3b2a1c-9d8f-4e7a-b6c5-d4e3f2a1b0c9", False)

    with respx.mock() as router:
        router.post("http://catalog.test/management/v1/table/acme-bronze$events/credentials").mock(
            return_value=httpx.Response(404, json={"code": 4, "detail": "Table not found"})
        )
        names = TableIds(by_location={normalise(named.dataset): "acme-bronze$events"})
        report = raise_listing_floors(settings, orphans=[named, unnamed], names=names, storage_options=StorageOptions())

    assert report.raised == []
    assert {r.dataset: r.refused for r in report.refused} == {
        unnamed.dataset: "no __manifest row names this dataset, so it is neither vended nor written",
        named.dataset: report.refused[-1].refused,
    }
    assert "TABLE_NOT_FOUND" in (report.refused[-1].refused or ""), report.refused


def test_a_location_the_catalog_governs_elsewhere_refuses_the_raise(monkeypatch: pytest.MonkeyPatch, readable_floor: None, tmp_path: Path) -> None:
    """The vend door answered 200 for a table at another location, so its credential does not cover this dataset.

    It reaches this pass only as a `MaintenanceDenied`: `GovernedElsewhere` is not named here, and escaping
    would abort the pass at the first crossing.
    """
    monkeypatch.setattr("maintenance.services.credentials.service_headers", lambda _settings: {})
    settings = _settings(tmp_path, dry_run=True, catalog_url="http://catalog.test")
    elsewhere = "s3://acme-bucket/medallion/bronze"

    with respx.mock() as router:
        router.post("http://catalog.test/management/v1/table/acme-bronze$events/credentials").mock(return_value=_vend_covering(elsewhere))
        orphans = [_orphan("s3://wh/0d9c6f3e-7b2a-4c1e-9f5d-2a8b3c4d5e6f", False)]
        names = TableIds(by_location={normalise(orphans[0].dataset): "acme-bronze$events"})
        report = raise_listing_floors(settings, orphans=orphans, names=names, storage_options=StorageOptions())

    assert report.raised == []
    assert elsewhere in (report.refused[0].refused or ""), report.refused


def test_a_dataset_already_raised_is_not_raised_again(door: Door, tmp_path: Path) -> None:
    """A COMMIT ALONE DOES NOT MOVE THE FLOOR — the manifests beneath it must then be cleaned.

    On a dataset the sweep may not maintain, nothing cleans them, so this pass would write a version per
    tick forever and reclaim nothing. Measured on the deployed estate 2026-09-19: the raise on
    `m2proof_silver$m2-proof-1788537252` ran twice before this guard existed, because the sweep refused
    that dataset for want of a write credential and its floor never moved.
    """
    uri, orphan, _ = _stranded_dataset(tmp_path)
    orphans = [OrphanFile(dataset=uri, path="_transactions/9-dead.txn", kind="transactions", reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime)]

    first = raise_listing_floors(_settings(tmp_path), orphans=orphans, names=door(orphans), storage_options=StorageOptions())
    version_after_first = lance.dataset(uri).version
    second = raise_listing_floors(_settings(tmp_path), orphans=orphans, names=door(orphans), storage_options=StorageOptions())

    assert [r.dataset for r in first.raised] == [uri]
    assert second.raised == []
    assert "already raised" in (second.refused[0].refused or "")
    assert lance.dataset(uri).version == version_after_first, "a second commit was written to a dataset whose floor cannot move"


@pytest.mark.parametrize("recorded", [pytest.param("a/ds", id="the-record-names-the-dataset"), pytest.param("a", id="the-record-names-a-location-holding-it")])
def test_a_trashed_dataset_is_refused_before_the_cap_and_gains_no_version(door: Door, tmp_path: Path, recorded: str) -> None:
    """A recoverable drop freezes its bytes until undrop or purge, and the trash record is what says so ([[LH-449]]).

    The refusal comes before the vend and before the cap: the trashed dataset sorts first and the cap is one, so a
    refusal that spent the slot would leave its live twin unraised.
    """
    trashed, trashed_orphan, _ = _stranded_dataset(tmp_path / "a")
    live, live_orphan, _ = _stranded_dataset(tmp_path / "b")
    record = trash.make_record("acme$frozen", uuid=str(uuid4()), location=str(tmp_path / recorded), dropped_by="owner", grace_days=7)
    trash.put(str(tmp_path), StorageOptions(), record)
    before = lance.dataset(trashed).version

    orphans = [
        OrphanFile(dataset=uri, path="_transactions/9-dead.txn", kind="transactions", reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime)
        for uri, orphan in ((trashed, trashed_orphan), (live, live_orphan))
    ]
    report = raise_listing_floors(_settings(tmp_path, max_per_tick=1), orphans=orphans, names=door(orphans), storage_options=StorageOptions())

    assert [r.dataset for r in report.raised] == [live], "the trashed dataset was taken, or took the only slot"
    assert [(r.dataset, r.refused) for r in report.refused] == [(trashed, f"in trash as acme$frozen (expires_at={record['expires_at']})")]
    assert report.capped == 0
    frozen = lance.dataset(trashed)
    assert (frozen.version, frozen.config().get(FLOOR_KEY)) == (before, None), "a version was written to a trash-frozen dataset"


def test_an_unreadable_trash_index_writes_and_plans_nothing(tmp_path: Path) -> None:
    """No dataset can then be shown to be outside the trash. The pass says why rather than raising out of the
    reconcile tick, whose drift report and purge read the trash for themselves."""
    uri, orphan, _ = _stranded_dataset(tmp_path)
    (tmp_path / "_trash").write_text("a file where the trash index should be")
    before = lance.dataset(uri).version

    orphans = [OrphanFile(dataset=uri, path="_transactions/9-dead.txn", kind="transactions", reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime)]
    report = raise_listing_floors(_settings(tmp_path), orphans=orphans, names=_named(orphans), storage_options=StorageOptions())

    assert (report.raised, report.refused, lance.dataset(uri).version) == ([], [], before)
    assert (report.error or "").startswith("the trash index could not be read"), report
