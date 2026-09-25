"""Raising the listing floor makes Lance reclaim residue it had gone permanently blind to ([[LH-094]]).

Owner ruling 2026-09-19 (`docs/DECISIONS.md`): rask does not delete the stranded bytes. It writes one
metadata-only commit, which moves the dataset's floor above them, and Lance's own
`cleanup_old_versions` does the deleting under its own rule on the next ordinary sweep.

THE FIRST TEST DRIVES REAL LANCE, not a fake, because the claim being made is about upstream's
behaviour and a fake would only restate the belief under test. It builds the estate's measured shape —
a dataset collapsed to one live version with an unreferenced file whose mtime is ABOVE the surviving
manifest's commit timestamp but in the past — and asserts the before/after: `cleanup_old_versions`
removes nothing, then after one config commit it removes the file and the rows still read.

The remaining tests drive the selection, which is where the judgement lives: which datasets are
eligible, which are refused, and what a dry run does.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pytest

from maintenance.services.compaction_executor import MaintenanceDenied
from maintenance.services.floor import FLOOR_KEY, FloorReport, raise_listing_floors
from maintenance.services.orphans import OrphanFile
from service_kit.lancekit.versions import committed_at


class _Settings:
    """The fields this pass reads. A real `MaintenanceSettings` would drag its whole env.

    `catalog_url` empty is the NO-VENDING-DOOR posture, which `write_options_for` handles by returning
    the ambient fallback and saying so. The tests that care about vending patch that function outright.
    """

    def __init__(self, *, enabled: bool = True, dry_run: bool = False, max_per_tick: int = 10) -> None:
        self.floor_raise_enabled = enabled
        self.floor_raise_dry_run = dry_run
        self.floor_raise_max_per_tick = max_per_tick
        self.catalog_url = ""
        self.s3_access_key_id = "test-key"


def _settings(**kwargs: Any) -> Any:
    return cast(Any, _Settings(**kwargs))


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

    orphan = Path(uri) / "data" / "stranded.lance"
    orphan.write_bytes(b"x" * 64)
    planted = floor + 0.5
    os.utime(orphan, (planted, planted))
    # So the planted mtime is in the past by the time cleanup runs; otherwise the raised floor lands
    # under it and the mechanism is measured against a file that has not happened yet.
    time.sleep(1.5)
    return uri, orphan, floor


def test_lance_reclaims_a_stranded_file_only_after_the_floor_is_raised(tmp_path: Path) -> None:
    """The mechanism, end to end, against pylance itself."""
    uri, orphan, floor = _stranded_dataset(tmp_path)
    assert orphan.stat().st_mtime > floor, "the fixture did not strand the file above the floor"

    before = lance.dataset(uri).cleanup_old_versions(older_than=dt.timedelta(seconds=0), delete_unverified=True)
    assert before.data_files_removed == 0, "Lance saw a file the floor should have hidden — the fixture is wrong, not Lance"
    assert orphan.exists()

    report = raise_listing_floors(
        _settings(),
        orphans=[
            OrphanFile(dataset=uri, path="data/stranded.lance", kind="data", size_bytes=64, reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime)
        ],
        storage_options={},
    )
    assert [r.dataset for r in report.raised] == [uri], report

    after = lance.dataset(uri).cleanup_old_versions(older_than=dt.timedelta(seconds=0), delete_unverified=True)
    assert after.data_files_removed == 1
    assert not orphan.exists()


def test_the_data_survives_the_commit_and_the_reclaim(tmp_path: Path) -> None:
    """The control that matters: this writes to a LIVE governed table, so the rows must be untouched."""
    uri, orphan, _ = _stranded_dataset(tmp_path)
    rows_before = lance.dataset(uri).to_table().num_rows

    raise_listing_floors(
        _settings(),
        orphans=[OrphanFile(dataset=uri, path="data/stranded.lance", kind="data", reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime)],
        storage_options={},
    )
    lance.dataset(uri).cleanup_old_versions(older_than=dt.timedelta(seconds=0), delete_unverified=True)

    assert lance.dataset(uri).to_table().num_rows == rows_before


def test_the_commit_is_a_config_change_lineage_already_knows_to_ignore(tmp_path: Path) -> None:
    """`UpdateConfig` is in `lineage.core.reconcile.MAINTENANCE_OPERATIONS`, so the sweep neither reports
    this version as a provenance hole nor back-fills it with a run that never existed. Asserted here
    because the property belongs to the pair, and neither module's own tests can see the other."""
    from lineage.core.reconcile import MAINTENANCE_OPERATIONS, read_storage_versions, read_version_operations

    uri, orphan, _ = _stranded_dataset(tmp_path)
    raise_listing_floors(
        _settings(),
        orphans=[OrphanFile(dataset=uri, path="data/stranded.lance", kind="data", reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime)],
        storage_options={},
    )

    versions = read_storage_versions(uri, {}) or []
    operations = read_version_operations(uri, {}, versions)
    newest = max(versions)
    assert operations[newest] == "UpdateConfig"
    assert operations[newest] in MAINTENANCE_OPERATIONS


def test_the_commit_names_itself_in_the_datasets_config(tmp_path: Path) -> None:
    """An operator reading the dataset must be able to tell this commit from a real one without the register."""
    uri, orphan, _ = _stranded_dataset(tmp_path)
    raise_listing_floors(
        _settings(),
        orphans=[OrphanFile(dataset=uri, path="data/stranded.lance", kind="data", reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime)],
        storage_options={},
    )

    assert FLOOR_KEY in lance.dataset(uri).config()


# --- selection: which datasets earn a commit -------------------------------------------------- #


def _orphan(dataset: str, reclaimable: bool | None, mtime: float = 2_000_000_000.0) -> OrphanFile:
    return OrphanFile(dataset=dataset, path="data/x.lance", kind="data", reclaimable_by_lance=reclaimable, mtime_epoch=mtime)


@pytest.fixture
def readable_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A floor every fixture orphan sits above, so selection is exercised without a dataset per URI.

    STUBBED RATHER THAN SKIPPED, because a dry run deliberately performs the REAL floor re-read — the
    same rule the trash purge states, that a preview runs the real pass and skips only the mutations.
    Left unstubbed, every fake URI here would be refused `floor unreadable` and the selection these
    tests exist to drive would never be reached.
    """
    monkeypatch.setattr("maintenance.services.floor._current_floor", lambda uri, storage_options: 1_000_000_000.0)


def _plan(orphans: list[OrphanFile], **kwargs: Any) -> FloorReport:
    return raise_listing_floors(_settings(dry_run=True, **kwargs), orphans=orphans, storage_options={})


def test_a_disabled_pass_selects_nothing(readable_floor: None) -> None:
    report = raise_listing_floors(_settings(enabled=False), orphans=[_orphan("s3://wh/a", False)], storage_options={})

    assert report.enabled is False
    assert report.raised == [] and report.refused == []


def test_an_orphan_that_time_will_clear_earns_no_commit(readable_floor: None) -> None:
    """`reclaimable_by_lance is True` is a file waiting out the 7-day rule. A commit buys it nothing."""
    assert _plan([_orphan("s3://wh/a", True)]).raised == []


def test_an_undecided_orphan_earns_no_commit(readable_floor: None) -> None:
    """``None`` is "the floor could not be read". The case for writing to a governed table is that the
    residue is PROVABLY permanent, and an unread floor proves nothing."""
    assert _plan([_orphan("s3://wh/a", None)]).raised == []


def test_one_undecided_sibling_disqualifies_the_whole_dataset(readable_floor: None) -> None:
    """All-or-nothing per dataset, because the commit is per dataset: raising the floor for one stranded
    file sweeps up its siblings whatever their class."""
    assert _plan([_orphan("s3://wh/a", False), _orphan("s3://wh/a", None)]).raised == []


def test_a_dataset_whose_orphans_are_all_stranded_is_selected(readable_floor: None) -> None:
    """The control. Without it, a pass that selected nothing would satisfy all three tests above."""
    plan = _plan([_orphan("s3://wh/a", False), _orphan("s3://wh/a", False)])

    assert [r.dataset for r in plan.raised] == ["s3://wh/a"]
    assert plan.raised[0].orphan_files == 2


def test_a_dry_run_writes_no_version(tmp_path: Path) -> None:
    uri, orphan, _ = _stranded_dataset(tmp_path)
    before = lance.dataset(uri).version

    report = raise_listing_floors(
        _settings(dry_run=True),
        orphans=[OrphanFile(dataset=uri, path="data/stranded.lance", kind="data", reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime)],
        storage_options={},
    )

    assert [r.dataset for r in report.raised] == [uri], "a dry run must still report the plan"
    assert report.raised[0].version is None
    assert lance.dataset(uri).version == before
    assert orphan.exists()


def test_a_floor_that_already_covers_its_orphans_is_refused(tmp_path: Path) -> None:
    """Acting on a stale report. Something committed between the scan and now, so the next sweep will
    reclaim unaided and a commit here buys only the phantom version."""
    uri, _, _ = _stranded_dataset(tmp_path)
    lance.dataset(uri).update_config({"unrelated": "commit"})

    report = raise_listing_floors(
        _settings(),
        orphans=[OrphanFile(dataset=uri, path="data/stranded.lance", kind="data", reclaimable_by_lance=False, mtime_epoch=1.0)],
        storage_options={},
    )

    assert report.raised == []
    assert [r.refused for r in report.refused] == ["floor already covers these orphans"]


def test_an_unreadable_dataset_is_refused_and_does_not_stop_the_others(tmp_path: Path) -> None:
    uri, orphan, _ = _stranded_dataset(tmp_path)

    report = raise_listing_floors(
        _settings(),
        orphans=[
            _orphan("s3://wh/does-not-exist", False),
            OrphanFile(dataset=uri, path="data/stranded.lance", kind="data", reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime),
        ],
        storage_options={},
    )

    assert [r.dataset for r in report.raised] == [uri]
    assert [r.refused for r in report.refused] == ["floor unreadable"]


def test_the_remainder_beyond_the_cap_is_reported(readable_floor: None) -> None:
    """A store rebuild strands EVERY dataset at once, so a truncated pass that said nothing would look
    complete and the next tick's smaller number would read as progress nobody made."""
    orphans = [_orphan(f"s3://wh/{i}", False) for i in range(5)]

    plan = _plan(orphans, max_per_tick=2)

    assert len(plan.raised) == 2
    assert plan.capped == 3


@pytest.mark.parametrize("dry_run", [True, False])
def test_the_report_states_which_mode_produced_it(dry_run: bool) -> None:
    """A plan and an act must never read alike — the same rule the trash purge's `dry_run` carries."""
    assert raise_listing_floors(_settings(dry_run=dry_run), orphans=[], storage_options={}).dry_run is dry_run


# --- the two defects the live run found -------------------------------------------------------- #


def test_the_commit_is_signed_by_the_vended_credential(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A write to a governed table goes through the vending door, like every other write here.

    `optimize.py` states the rule for the rewrite — "compacting locally would perform it under whatever
    credential opened `ds`, which on a denied table is the deployment's ambient key. That is the bypass
    this class exists to stop." A config commit is a write to the same table, so the same rule binds it.
    Found by deploying: the first version of this signed with the ambient options while the sweep, on
    the very same dataset, refused for want of a vended credential.
    """
    uri, orphan, _ = _stranded_dataset(tmp_path)
    seen: list[dict[str, str]] = []

    def _vended(dataset_uri: str, settings: Any, *, fallback: dict[str, str], declared_table_id: str | None = None) -> dict[str, str]:
        seen.append(fallback)
        return {"scoped": "yes"}

    monkeypatch.setattr("maintenance.services.floor.write_options_for", _vended)
    opened: list[dict[str, str]] = []
    real = lance.dataset

    def _record(dataset_uri: str, *args: Any, **kwargs: Any) -> Any:
        opened.append(kwargs.get("storage_options") or {})
        return real(dataset_uri, *args, **{**kwargs, "storage_options": {}})

    monkeypatch.setattr(lance, "dataset", _record)

    raise_listing_floors(
        _settings(),
        orphans=[OrphanFile(dataset=uri, path="data/stranded.lance", kind="data", reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime)],
        storage_options={"ambient": "yes"},
    )

    assert seen == [{"ambient": "yes"}], "the ambient options must be offered only as the vend's fallback"
    assert {"scoped": "yes"} in opened, "the commit was opened with something other than the vended credential"


def test_a_catalog_denial_refuses_the_raise(monkeypatch: pytest.MonkeyPatch, readable_floor: None) -> None:
    """A 403 means this identity may not write this table. Committing anyway is the bypass."""

    def _denied(*args: Any, **kwargs: Any) -> dict[str, str]:
        raise MaintenanceDenied("the catalog REFUSED a write credential for t (403)")

    monkeypatch.setattr("maintenance.services.floor.write_options_for", _denied)

    report = raise_listing_floors(_settings(), orphans=[_orphan("s3://wh/denied", False)], storage_options={})

    assert report.raised == []
    assert "REFUSED a write credential" in (report.refused[0].refused or "")


def test_a_denial_is_reported_by_the_DRY_RUN_too(monkeypatch: pytest.MonkeyPatch, readable_floor: None) -> None:
    """A preview that lists a table the catalog will refuse tells an operator the opposite of the truth."""

    def _denied(*args: Any, **kwargs: Any) -> dict[str, str]:
        raise MaintenanceDenied("the catalog REFUSED a write credential for t (403)")

    monkeypatch.setattr("maintenance.services.floor.write_options_for", _denied)

    report = raise_listing_floors(_settings(dry_run=True), orphans=[_orphan("s3://wh/denied", False)], storage_options={})

    assert report.raised == []
    assert len(report.refused) == 1


def test_a_dataset_already_raised_is_not_raised_again(tmp_path: Path) -> None:
    """A COMMIT ALONE DOES NOT MOVE THE FLOOR — the manifests beneath it must then be cleaned.

    On a dataset the sweep may not maintain, nothing cleans them, so this pass would write a version per
    tick forever and reclaim nothing. Measured on the deployed estate 2026-09-19: the raise on
    `m2proof_silver$m2-proof-1788537252` ran twice before this guard existed, because the sweep refused
    that dataset for want of a write credential and its floor never moved.
    """
    uri, orphan, _ = _stranded_dataset(tmp_path)
    orphans = [OrphanFile(dataset=uri, path="data/stranded.lance", kind="data", reclaimable_by_lance=False, mtime_epoch=orphan.stat().st_mtime)]

    first = raise_listing_floors(_settings(), orphans=orphans, storage_options={})
    version_after_first = lance.dataset(uri).version
    second = raise_listing_floors(_settings(), orphans=orphans, storage_options={})

    assert [r.dataset for r in first.raised] == [uri]
    assert second.raised == []
    assert "already raised" in (second.refused[0].refused or "")
    assert lance.dataset(uri).version == version_after_first, "a second commit was written to a dataset whose floor cannot move"
