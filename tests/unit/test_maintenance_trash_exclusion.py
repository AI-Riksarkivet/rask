"""diff2 F6(d) — the sweep must not maintain datasets that are IN THE TRASH.

A recoverable drop deregisters the table's ``__manifest`` row and files a trash record. It moves no
byte: measured against the shipped ``dir`` backend, the dataset directory keeps its ``_versions/``
child — and that marker is the ONLY thing ``discover_datasets`` consults to decide "this is a Lance
dataset". So a dropped table was rediscovered on the very next tick, indistinguishable from a live
one.

The consequence is not a wasted cycle, it is silent irreversible mutation of data the owner was told
is frozen:

    Karin drops `silver$htr_lines_2019` on a Friday. Grace is 7 days, so the UI tells her she can
    undrop until next Friday. The cron fires every 30 minutes (chart/values-prod.yaml). Over the
    weekend the sweep compacts that dataset, remaps its indices and runs `cleanup_old_versions` on
    it, repeatedly. Monday she undrops and gets a table back — but not the one she dropped: the
    version history she was trying to recover to has been pruned, and the sweep's summary counted it
    as a successful maintenance pass.

These tests drive the REAL ``run_sweep`` rather than the partition helper, because the defect is
positional: the exclusion has to sit between the whole-estate base-ref pre-pass and the compaction
loop, and a test that called a helper directly would pass with the filter in the one place that
re-opens a different data-loss bug (see the shallow-clone test below).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest
from maintenance.core.config import MaintenanceSettings
from maintenance.services.optimize import Discovery

from service_kit.lakehouse import trash


def _dataset(path: Path) -> str:
    """A two-version Lance dataset — enough for compaction to have something to do."""
    uri = str(path)
    table = pa.table({"n": pa.array([1, 2], pa.int64())})
    lance.write_dataset(table, uri, mode="overwrite")
    lance.write_dataset(table, uri, mode="append")
    return uri


@pytest.fixture
def settings(tmp_path: Path) -> MaintenanceSettings:
    control = tmp_path / "control"
    control.mkdir()
    return MaintenanceSettings(
        MAINTENANCE_CONTROL_ROOT=str(control),
        MAINTENANCE_POLICY_ROOT=str(control),
    )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    settings: MaintenanceSettings,
    uris: list[str],
) -> tuple[list[Any], list[str]]:
    """Run the real sweep over ``uris``; return (results, the URIs Lance was actually asked to compact)."""
    from maintenance.services import sweep as sweep_mod

    compacted: list[str] = []
    real = lance.dataset(uris[0]).optimize.__class__.compact_files

    def _spy(self: object, *args: object, **kwargs: object) -> object:
        # `self` is the DatasetOptimizer; its `_dataset.uri` is the dataset being rewritten.
        compacted.append(str(getattr(getattr(self, "_dataset", None), "uri", "?")))
        return real(self, *args, **kwargs)  # ty: ignore[invalid-argument-type] — a spy is deliberately untyped

    monkeypatch.setattr(sweep_mod, "_s3fs", lambda _s: None)
    monkeypatch.setattr(sweep_mod, "discover_datasets", lambda _fs, _bucket: Discovery(uris=list(uris)))
    lance.dataset(uris[0]).optimize.__class__.compact_files = _spy  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
    try:
        results = sweep_mod.run_sweep(settings)
    finally:
        lance.dataset(uris[0]).optimize.__class__.compact_files = real  # type: ignore[method-assign]
    return results, compacted


def test_a_trashed_dataset_is_not_compacted_and_says_so(tmp_path: Path, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE ACCEPTANCE CRITERION. The dropped dataset is left alone, and the tick reports why."""
    live = _dataset(tmp_path / "live.lance")
    dropped = _dataset(tmp_path / "dropped.lance")

    # Exactly what the catalog's recoverable drop writes: the bytes stay where they are, and a record
    # under the control root points at them.
    record = trash.make_record("silver$htr_lines_2019", kind="table", location=dropped, dropped_by="user:karin", grace_days=7)
    trash.put(str(settings.resolved_control_root), {}, record)

    results, compacted = _run(monkeypatch, settings, [live, dropped])

    assert dropped not in compacted, "the sweep rewrote a dataset the grace window promises is frozen"
    assert live in compacted, "the exclusion caught a LIVE dataset — the path comparison is too broad"

    excluded = [r for r in results if r.trashed]
    assert [r.uri for r in excluded] == [dropped]
    # The reason names the record AND its deadline: an exclusion that has outlived its deadline is a
    # record the purge keeps refusing, i.e. a PERMANENT exclusion, and that must be readable from the
    # summary rather than inferred.
    assert "silver$htr_lines_2019" in (excluded[0].trashed or "")
    assert "expires_at=" in (excluded[0].trashed or "")


def test_an_expired_but_unpurged_record_still_freezes_its_dataset(tmp_path: Path, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A passed deadline is permission for the PURGE to reclaim, not permission for the sweep to rewrite.

    `undrop` deliberately does not consult the clock, so a record whose deadline has passed but which
    nothing has reclaimed still describes recoverable bytes. Keying the exclusion on `expired()`
    instead of on every record would put exactly those datasets — the ones most likely to be stuck —
    back under the compactor.
    """
    dropped = _dataset(tmp_path / "long_expired.lance")
    record = trash.make_record("silver$old", kind="table", location=dropped, dropped_by="user:karin", grace_days=7)
    record["expires_at"] = "2020-01-01T00:00:00+00:00"
    trash.put(str(settings.resolved_control_root), {}, record)

    results, compacted = _run(monkeypatch, settings, [dropped])

    assert compacted == [], "an expired-but-unpurged record was rewritten"
    assert [r.uri for r in results if r.trashed] == [dropped]


def test_a_trashed_clone_still_protects_its_live_source(tmp_path: Path, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE POSITIONAL TRAP, and the reason this suite drives the real sweep.

    The exclusion must be applied AFTER `base_refs.protected_roots` runs over the FULL discovered list.
    A trashed shallow CLONE's manifest `base_paths` are the only evidence in the estate that its LIVE
    source must not be compacted (#114/#128d) — the source carries no flag of its own. Filter the
    trashed dataset out before the pre-pass and that protection disappears, so the sweep destroys the
    live source's data files: a worse bug than the one being fixed, reached by putting the right
    filter in the wrong place.

    Asserted through the pre-pass's own output rather than by constructing a real shallow clone, which
    needs a Lance feature this suite does not otherwise depend on.
    """
    from maintenance.services import sweep as sweep_mod

    from service_kit.lakehouse import base_refs

    source = _dataset(tmp_path / "source.lance")
    clone = _dataset(tmp_path / "clone.lance")
    trash.put(
        str(settings.resolved_control_root),
        {},
        trash.make_record("silver$clone", kind="table", location=clone, dropped_by="user:karin", grace_days=7),
    )

    seen: list[list[str]] = []
    real_protected = base_refs.protected_roots
    monkeypatch.setattr(
        sweep_mod.base_refs,
        "protected_roots",
        # Forwards **kw so the stub cannot fall behind the real signature: the sweep threads the
        # service's configured Lance session, and a stub that silently dropped it would keep passing
        # while the call it stands in for had moved on.
        lambda uris, options=None, **kw: (seen.append(list(uris)), real_protected(uris, options, **kw))[1],
    )
    _run(monkeypatch, settings, [source, clone])

    assert seen, "the base-ref pre-pass did not run"
    assert clone in seen[0], (
        "the trashed dataset was filtered out BEFORE the base-ref pre-pass. A trashed shallow clone's "
        "base_paths are the only thing protecting its live source from compaction — removing it here "
        "re-opens #114 from the trash side."
    )


def test_an_unreadable_trash_index_aborts_the_tick(tmp_path: Path, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail toward NOT rewriting, exactly as the policy registry already does.

    This block used to swallow every error, which was right while its only consumer was a log line.
    Now the same read decides which datasets may be rewritten, so swallowing would silently restore
    the defect this work closes: an unreadable index means an empty exclusion set means maintain
    everything, including the frozen data. The precedent is `compaction_policies_unreadable_tick_aborted`
    forty lines above — a protective registry we cannot read at all stops the tick, and the next cron
    fire retries.
    """
    from maintenance.services import sweep as sweep_mod

    live = _dataset(tmp_path / "live.lance")

    def _boom(*_a: object, **_k: object) -> list[dict[str, Any]]:
        raise OSError("control root unreachable")

    monkeypatch.setattr(sweep_mod.trash, "list_all", _boom)
    with pytest.raises(OSError, match="control root unreachable"):
        _run(monkeypatch, settings, [live])


def test_the_summary_counts_and_names_the_exclusions(tmp_path: Path, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tick that maintains 1 of 2 datasets must say what happened to the other one.

    Its own summary line rather than a fold into `skipped` or `refused`: `skipped` means "not this
    tick" and would inflate the policy-cadence reading for something that lasts until undrop or purge,
    and `refused` is about a dataset's LAYOUT. This is about its governance state.
    """
    from maintenance.services import sweep as sweep_mod

    live = _dataset(tmp_path / "live.lance")
    dropped = _dataset(tmp_path / "dropped.lance")
    trash.put(
        str(settings.resolved_control_root),
        {},
        trash.make_record("silver$gone", kind="table", location=dropped, dropped_by="user:karin", grace_days=7),
    )

    results, _ = _run(monkeypatch, settings, [live, dropped])
    summary = sweep_mod.summarize(results)

    assert summary["trashed"] == 1
    assert list(summary["trashed_datasets"]) == [dropped]
    assert summary["skipped"] == 0, "a trash exclusion was folded into `skipped`"
    assert summary["refused"] == 0, "a trash exclusion was folded into `refused`"


def test_a_namespace_record_carrying_no_location_excludes_nothing(tmp_path: Path, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A trashed NAMESPACE and a declared-only table both record `location=""`.

    Normalising the empty string yields the empty string, which is a prefix of every path — so an
    unguarded set comprehension would build an exclusion entry that matches nothing by equality but
    invites a future prefix-match refactor to exclude the entire estate. Pin the guard.
    """
    live = _dataset(tmp_path / "live.lance")
    trash.put(
        str(settings.resolved_control_root),
        {},
        trash.make_record("silver", kind="namespace", location="", dropped_by="user:karin", grace_days=7),
    )

    results, compacted = _run(monkeypatch, settings, [live])

    assert compacted == [live], "a location-less namespace record froze a live dataset"
    assert [r for r in results if r.trashed] == []
