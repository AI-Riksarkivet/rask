"""The bucket walk must not invent a coverage gap, and must not lose a bucket to a vanished prefix.

the lakehouse register, row H11 (drained 2026-09-10; in git history).

A subtree the walk STOPS INSIDE becomes an `IncompleteScan`, and `purge.report_is_clean` blocks on
that — so a prefix that will never hold a dataset blocks reclamation exactly as hard as a manifest
nobody could read, and blocks it forever. The walk cannot tell "no dataset here" from "did not look
deep enough", which is why it must not enter subtrees known to hold no data.

MEASURED LIVE 2026-09-08, and the measurement refuted the row's first version, which had assumed the
`depth limit reached` notes meant hidden unmaintained data:

    max_depth=3   datasets 30   stopped 49
    max_depth=6   datasets 30   stopped  0
    under models/, walked to depth 8: 0 datasets

Not one dataset hides below the bound. The 59 stopped prefixes of the primary bucket were 49
`models/<run>/<id>/` training artefacts and 10 `_lineage_outbox/<event>.json/<id>/` bookkeeping
entries — `_lineage_outbox` being the only one of the five control prefixes the walk did not skip.

Walking the outbox also RACED ITS DRAIN: it is emptied continuously, so a directory named by one
listing was gone before the descent, and a depth-4 walk died `FileNotFoundError`. Both callers catch
per BUCKET, so that one vanished sub-prefix cost a whole bucket its maintenance for the tick.

The depth bound stays a POLICY LEVER rather than a rescue — it reveals no hidden data, and its default
is unchanged. It is bounded at both ends because the walk is the sweep's dominant cost:
`sweep.py::_protected_roots` opens every discovered dataset in every bucket before one is compacted.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pyarrow.fs as pafs
import pytest
from pydantic import SecretStr

from maintenance.core.config import MaintenanceSettings
from maintenance.services import sweep as sweep_mod
from maintenance.services.optimize import Discovery, discover_datasets


class _NoBuckets:
    """The S3 read half, so the store categories complete and only the discovery seam is under test."""

    def list_buckets(self) -> dict[str, Any]:
        return {"Buckets": []}


def _nested_dataset(root: Path, depth: int) -> None:
    """One dataset `depth` directory levels below `root` (depth=1 is a direct child)."""
    path = root.joinpath(*[f"lvl{i}" for i in range(1, depth)], "t.lance")
    lance.write_dataset(pa.table({"id": [1, 2]}), str(path))


# --------------------------------------------------------------------------- #
# the bound itself — the boundary pair, not one side of it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("depth", "bound", "found"), [(3, 3, True), (4, 3, False), (4, 4, True)])
def test_the_walk_reaches_exactly_as_deep_as_it_is_told(tmp_path: Path, depth: int, bound: int, found: bool) -> None:
    """Exactly the limit, one over, and one over with the limit raised — the boundary checklist.

    The third case is the one that matters for § H11: the estate's 70 unreachable prefixes become
    reachable by CONFIGURATION, which is precisely what no caller can currently do.
    """
    _nested_dataset(tmp_path, depth)

    result = discover_datasets(pafs.LocalFileSystem(), str(tmp_path), max_depth=bound)

    assert bool(result.uris) is found, f"depth {depth} under bound {bound}: {result.uris or result.truncated}"
    assert bool(result.truncated) is not found, "a prefix the walk stopped at must be recorded, never dropped"


# --------------------------------------------------------------------------- #
# the lever reaches both call sites
# --------------------------------------------------------------------------- #


def test_the_setting_exists_and_is_bounded() -> None:
    """A depth of 0 maintains nothing and an unbounded one opens every dataset in the estate on a walk
    that is already the sweep's dominant cost. Both ends are refused."""
    assert MaintenanceSettings(discovery_max_depth=7).discovery_max_depth == 7
    for refused in (0, 17):
        with pytest.raises(ValueError):
            MaintenanceSettings(discovery_max_depth=refused)


def _capture_depth(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[int | None]:
    seen: list[int | None] = []

    def _spy(_fs: Any, _bucket: str, *, max_depth: int | None = None) -> Discovery:
        seen.append(max_depth)
        return Discovery()

    monkeypatch.setattr(module, "discover_datasets", _spy)
    return seen


def test_the_sweep_walks_the_configured_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_depth(monkeypatch, sweep_mod)
    settings = MaintenanceSettings(discovery_max_depth=6)

    sweep_mod._discover_all(pafs.LocalFileSystem(), ["b"], max_depth=settings.discovery_max_depth)

    assert seen == [6], "the sweep took the function default instead of the configured bound"


def test_the_sweep_reports_the_bound_it_actually_used(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The truncation warning stated `max_depth: 3` as a LITERAL beside a call that passed nothing, so
    the log would have kept saying 3 while the walk used something else. A number a reader cannot trust
    is worse than no number."""

    def _truncating(_fs: Any, _bucket: str, *, max_depth: int | None = None) -> Discovery:
        return Discovery(truncated=["s3://b/deep/"])

    monkeypatch.setattr(sweep_mod, "discover_datasets", _truncating)

    with caplog.at_level(logging.WARNING):
        sweep_mod._discover_all(pafs.LocalFileSystem(), ["b"], max_depth=6)

    truncation = [r for r in caplog.records if r.message == "maintenance_discovery_truncated"]
    assert truncation, "the truncation warning stopped firing"
    assert getattr(truncation[0], "max_depth", None) == 6


def test_the_reconciler_walks_the_configured_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other call site. It is the one that files the `IncompleteScan` blocking the purge, so a
    bound it cannot be told is a gate nobody can open."""
    from maintenance.services import reconcile as reconcile_mod

    seen = _capture_depth(monkeypatch, reconcile_mod)
    settings = MaintenanceSettings(
        s3_bucket="lance-catalog",
        s3_access_key_id="unit",
        s3_secret_access_key=SecretStr("unit"),
        orphan_scan_enabled=True,
        discovery_max_depth=5,
    )
    (tmp_path / "control").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    asyncio.run(
        reconcile_mod.reconcile(
            settings,
            None,
            warehouses_enabled=False,
            control_root=f"file://{tmp_path / 'control'}",
            namespace_root=f"file://{tmp_path / 'data'}",
            bucket_client=_NoBuckets(),
        )
    )

    assert seen and set(seen) == {5}, f"the reconciler took the function default instead of the configured bound: {seen}"


# --------------------------------------------------------------------------- #
# what the walk must never descend into, and what it must survive
# --------------------------------------------------------------------------- #


def test_the_lineage_outbox_is_never_walked(tmp_path: Path) -> None:
    """It is control-plane bookkeeping in the same class as `_warehouses` and `_trash`, and it was the
    only one of the five not skipped.

    Measured 2026-09-08 on the deployed estate: 10 of the primary bucket's 59 truncated prefixes were
    `_lineage_outbox/<event>.json/<id>/` — dead ends filed as `IncompleteScan`, which BLOCKS the purge
    for a coverage gap that cannot exist. No dataset is ever written under it.
    """
    (tmp_path / "_lineage_outbox" / "ev@COMPLETE.json" / "0cadeb18").mkdir(parents=True)
    lance.write_dataset(pa.table({"id": [1]}), str(tmp_path / "real.lance"))

    # The bound is set so that DESCENDING would truncate: `_lineage_outbox/ev@COMPLETE.json` sits at
    # depth 2 and has a child. A walk that skips the outbox truncates nothing; one that enters it
    # files a dead end. Without this the assertion passes on any bound deep enough to exhaust the tree.
    result = discover_datasets(pafs.LocalFileSystem(), str(tmp_path), max_depth=2)

    assert [u.rsplit("/", 1)[-1] for u in result.uris] == ["real.lance"]
    assert result.truncated == [], f"the walk descended into control-plane bookkeeping: {result.truncated}"


def test_a_prefix_that_vanishes_mid_walk_does_not_lose_the_bucket(tmp_path: Path) -> None:
    """The estate rewrites and reclaims continuously, so a directory named by one listing can be gone
    before the walk descends into it. That raised straight out of `discover_datasets` — and BOTH
    callers catch per BUCKET, so one vanished sub-prefix cost a whole bucket its maintenance for the
    tick, reported as `compaction_bucket_skipped` / `dataset discovery failed`.

    Measured 2026-09-08: a depth-4 walk of the primary bucket died `FileNotFoundError` on a
    `_lineage_outbox` entry the drain had already removed.
    """
    (tmp_path / "ns" / "gone").mkdir(parents=True)
    lance.write_dataset(pa.table({"id": [1]}), str(tmp_path / "ns" / "real.lance"))

    class _VanishingFS(pafs.LocalFileSystem):
        def get_file_info(self, arg: object) -> object:
            if isinstance(arg, pafs.FileSelector) and arg.base_dir.endswith("/gone"):
                raise FileNotFoundError(arg.base_dir)
            return super().get_file_info(arg)

    result = discover_datasets(_VanishingFS(), str(tmp_path), max_depth=6)

    assert [u.rsplit("/", 1)[-1] for u in result.uris] == ["real.lance"], "one vanished prefix must not cost the bucket"


def test_a_MISSING_BUCKET_still_raises(tmp_path: Path) -> None:
    """The other side of it, and the reason the tolerance is scoped to nested prefixes only: a bucket
    that does not exist must stay distinguishable from an empty one. Both callers rely on the raise —
    swallowing it would report "no datasets here" for a tenant bucket nobody can read, which is the
    "0 that means we did not look" this module's docstrings forbid."""
    with pytest.raises(FileNotFoundError):
        discover_datasets(pafs.LocalFileSystem(), str(tmp_path / "no-such-bucket"), max_depth=3)


# --------------------------------------------------------------------------- #
# a prefix PROVEN to hold no dataset is not a coverage gap
# --------------------------------------------------------------------------- #


def test_a_directory_of_FILES_at_the_bound_is_not_a_coverage_gap(tmp_path: Path) -> None:
    """The walk can tell "no dataset here" from "did not look deep enough" — for the case it can prove.

    A Lance dataset IS a directory with a `_versions/` child (`objectfs.is_lance_dataset_root`, the
    estate's one definition of the marker). So a directory whose children are all FILES cannot hide
    one, at any depth. Reporting it as an unscanned subtree is inventing a gap.

    THE COST OF INVENTING IT IS NOT COSMETIC. `purge.report_is_clean` blocks on any `IncompleteScan`,
    so a prefix that will never hold a dataset blocks reclamation exactly as hard as a manifest nobody
    could read — and blocks it forever, because nothing about that prefix will ever change.

    Measured on the deployed estate 2026-09-10: 64 of 64 `incomplete` units were `depth limit reached`,
    the majority `s3://lance-catalog/models/<model>/<hash>` — model artefacts, which are files. This
    file's own header records the other half of the proof: walking `models/` to depth 8 finds 0
    datasets. The skip list handles the control prefixes it knows by NAME; this handles the general
    case by evidence.
    """
    artefacts = tmp_path / "models" / "churn" / "060ab43a89c0"
    artefacts.mkdir(parents=True)
    (artefacts / "weights.bin").write_bytes(b"x")
    (artefacts / "card.md").write_text("model card")

    result = discover_datasets(pafs.LocalFileSystem(), str(tmp_path), max_depth=3)

    assert not result.uris, "no dataset exists here"
    assert not result.truncated, f"a directory of files was reported as an unscanned subtree: {result.truncated}"


def test_a_directory_of_DIRECTORIES_at_the_bound_is_STILL_a_gap(tmp_path: Path) -> None:
    """The other half, and the one that keeps the fix honest.

    A subdirectory below the bound genuinely may hold a dataset — the walk did not look, and cannot
    say. Suppressing that would trade a false gap for a false clean, which is strictly worse: the
    first blocks a purge, the second lets one run over ground nobody scanned.
    """
    deep = tmp_path / "tenant" / "zone" / "unscanned" / "maybe_a_dataset"
    deep.mkdir(parents=True)

    result = discover_datasets(pafs.LocalFileSystem(), str(tmp_path), max_depth=3)

    assert not result.uris
    assert result.truncated, "a subtree the walk did not enter must still be recorded"


def test_a_distributed_runs_STAGING_set_is_not_discovered_as_a_governed_dataset(tmp_path: Path) -> None:
    """CONTRACT: `_staging` is a control prefix — the walk never reports a run's scratch as a dataset.

    A distributed stage lands its output in `<destination>/_staging/<idempotency-key>` before one merge
    converges it (LH-007). Once the destination exists that set is already invisible, because the walk
    descends a dataset root's children ONLY into `tree/`. But on the run that CREATES the destination
    there is a window where the parent is a plain directory, and a crash inside it leaves a real Lance
    dataset the walk would find, compact, and count among the estate's governed tables.

    Named here rather than relied on by accident: the exclusion has to hold in both states, and
    `_staging` carries ONE underscore so the `__`-prefix rule does not cover it.
    """
    import pyarrow.fs as pafs

    from maintenance.services.optimize import discover_datasets

    root = tmp_path / "bucket"
    # The window: a destination that is not yet a dataset, holding a staged one.
    staged = root / "silver" / "_staging" / "run-1"
    staged.mkdir(parents=True)
    (staged / "_versions").mkdir()
    # A real governed sibling, so the walk is proven to still find what it should.
    live = root / "gold"
    live.mkdir(parents=True)
    (live / "_versions").mkdir()

    found = discover_datasets(pafs.LocalFileSystem(), str(root), max_depth=6)

    assert any(uri.endswith("/gold") for uri in found.uris), f"the walk stopped finding live datasets: {found.uris}"
    assert not [uri for uri in found.uris if "_staging" in uri], f"a run's staging set was reported as governed: {found.uris}"
