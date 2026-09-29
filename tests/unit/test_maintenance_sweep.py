"""Unit tests for the compaction service core — infra-free (no S3, no Lance).

Pins the two pieces of logic that aren't just Lance calls: dataset discovery (skip the catalog's
``__manifest`` + non-directories) and the sweep summary aggregation.
"""

from __future__ import annotations

from typing import Any, cast

import pyarrow.fs as pafs

from maintenance.services.optimize import DatasetResult, discover_datasets
from maintenance.services.sweep import summarize


def _dir(path: str) -> pafs.FileInfo:
    return pafs.FileInfo(path, pafs.FileType.Directory)


class _FakeFS:
    """Path-aware fake covering the two calls discover_datasets makes: listing a prefix
    (FileSelector) and probing a single path (the ``_versions`` dataset marker)."""

    def __init__(self, tree: dict[str, list[pafs.FileInfo]]) -> None:
        self._tree = tree

    def get_file_info(self, selector: Any) -> Any:
        if isinstance(selector, pafs.FileSelector):
            return self._tree.get(selector.base_dir, [])
        for infos in self._tree.values():
            for info in infos:
                if info.path == selector:
                    return info
        return pafs.FileInfo(selector, pafs.FileType.NotFound)


def test_discover_skips_manifest_and_non_dirs() -> None:
    fs = _FakeFS(
        {
            "lance-catalog": [
                _dir("lance-catalog/abcd_ns$table"),
                _dir("lance-catalog/__manifest"),  # bookkeeping → skip
                pafs.FileInfo("lance-catalog/loose.txt", pafs.FileType.File),  # not a dataset → skip
                _dir("lance-catalog/efgh_gold$catalog"),
            ],
            "lance-catalog/abcd_ns$table": [_dir("lance-catalog/abcd_ns$table/_versions")],
            "lance-catalog/efgh_gold$catalog": [_dir("lance-catalog/efgh_gold$catalog/_versions")],
        }
    )
    uris = discover_datasets(cast(Any, fs), "lance-catalog").uris
    assert uris == ["s3://lance-catalog/abcd_ns$table", "s3://lance-catalog/efgh_gold$catalog"]


def test_summarize_reports_refusals_as_their_own_category() -> None:
    """#64 — a REFUSED dataset must be its own line, never folded into `errors` or `skipped`.

    It is neither: a skip means "not this tick" (folding a permanent refusal in inflates the cadence
    count), and an error means "something failed" (nothing did — the pass declined before touching a
    byte, and the lineage layer treats errors as noise it can drop). Burying it is precisely what
    made a shallow clone's silent full materialization invisible in the cron response.
    """
    results = [
        DatasetResult(uri="s3://b/ok", fragments_removed=3, old_versions_removed=2),
        DatasetResult(uri="s3://b/clone", refused="unsupported manifest feature flags: 16 (base_paths (shallow clone / multi-base))"),
        DatasetResult(uri="s3://b/skipped", skipped="policy_interval"),
        DatasetResult(uri="s3://b/broken", error="maintain: boom"),
    ]

    summary = summarize(results)

    assert summary["refused"] == 1
    # …and it kept the WHY, keyed by URI — a count alone cannot tell an operator which flag stopped
    # maintenance or on which dataset.
    assert summary["refusals"] == {"s3://b/clone": results[1].refused}
    # Crucially, it did NOT land anywhere else.
    assert summary["errors"] == {"s3://b/broken": "maintain: boom"}, "a refusal was reported as an error"
    assert summary["skipped"] == 1, "a refusal was counted as a policy skip"
    assert summary["datasets"] == 4


def test_a_tick_aborted_before_the_loop_still_counts_as_started(monkeypatch: Any) -> None:
    """`compaction.runs` fired only AFTER the sweep loop, so a pass killed
    mid-flight was observationally identical to a tick that never arrived. The started counter must
    fire before anything abortable — here the policy-registry read, whose failure aborts the tick by
    design — so started-minus-completed is a real lost-pass count."""
    import pytest

    from maintenance.core.config import MaintenanceSettings
    from maintenance.services import sweep as sweep_mod

    calls: list[str] = []
    monkeypatch.setattr(sweep_mod, "record_run_started", lambda: calls.append("started"))
    monkeypatch.setattr(sweep_mod, "record_run", lambda: calls.append("completed"))
    monkeypatch.setattr(sweep_mod, "_s3fs", lambda _settings: object())

    def _registry_down(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("policy registry unreachable")

    monkeypatch.setattr(sweep_mod.maintenance_policies, "list_policies", _registry_down)

    with pytest.raises(RuntimeError):
        sweep_mod.run_sweep(MaintenanceSettings())

    assert calls == ["started"], "an aborted tick must be visible as started-but-not-completed"


def test_on_cron_single_flight_skips_an_overlapping_sweep(monkeypatch: Any) -> None:
    """A cron tick that finds a prior sweep still in flight SKIPS instead of starting a SECOND concurrent
    sweep — two sweeps would race compact_files()/cleanup_old_versions() on the same datasets. The sweep is
    unbounded (every dataset) so overlap is real once it outlasts the cron interval."""
    import asyncio
    import types

    from maintenance.api import routes

    ran: list[int] = []
    monkeypatch.setattr(routes, "run_sweep", lambda _settings: (ran.append(1), [])[1])

    async def _noop_emit(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(routes, "emit_sweep_lineage", _noop_emit)
    monkeypatch.setattr(routes, "summarize", lambda _results: {"status": "ok", "datasets": 0})
    # `work_topic=""` selects the SERIAL lane, which is what this test is about — the single-flight
    # guard is what stands in for a lease there. It holds on the queue lane too (two concurrent planners
    # would enqueue every dataset twice), but there the tick is bounded and duplicate units are merely
    # wasted rather than racing.
    settings = cast(Any, types.SimpleNamespace(delimiter="$", work_topic=""))

    async def while_a_sweep_is_running() -> dict:
        async with routes._sweep_lock:  # simulate the previous tick's sweep still holding the lock
            return await routes.on_cron(settings, cast(Any, object()), None)

    skipped = asyncio.run(while_a_sweep_is_running())
    assert skipped["status"] == "skipped" and ran == [], "an overlapping tick must NOT start a 2nd sweep"

    # once the lock is free again, a tick runs the sweep exactly once
    ok = asyncio.run(routes.on_cron(settings, cast(Any, object()), None))
    assert ok["status"] == "ok" and ran == [1]


def test_a_REFUSED_dataset_is_not_stamped_as_freshly_maintained(monkeypatch: Any) -> None:
    """A refusal carries `error=None`, so the cadence stamp treated it as a successful pass.

    `optimize.compact_one` returns `DatasetResult(uri=…, refused=…)` with no error — a refusal is not
    a failure, and that distinction is deliberate everywhere else. But the stamp gate read
    `result.error is None` and therefore recorded a dataset the sweep can NEVER maintain as freshly
    maintained. For the whole `compact_interval_hours` window it then reported as a transient
    `policy_interval` skip rather than a standing refusal: a permanent condition wearing a temporary
    label, on the one surface an operator would use to notice it.

    Measured context: 17 datasets in this estate are refused on manifest flag 16, and they are exactly
    the ones that need compaction.
    """
    from maintenance.core.config import MaintenanceSettings
    from maintenance.services import sweep as sweep_mod

    policy = {"id": "p1", "kind": "table", "compact_interval_hours": 24}
    stamped: list[str] = []

    monkeypatch.setattr(sweep_mod, "_s3fs", lambda _settings: object())
    monkeypatch.setattr(sweep_mod.maintenance_policies, "list_policies", lambda *_a, **_k: [policy])
    monkeypatch.setattr(sweep_mod.maintenance_policies, "resolve_policy", lambda *_a, **_k: policy)
    monkeypatch.setattr(sweep_mod.maintenance_policies, "read_state", lambda *_a, **_k: None)
    monkeypatch.setattr(sweep_mod.maintenance_policies, "write_state", lambda _root, _o, _p, uri, _at: stamped.append(uri))
    monkeypatch.setattr(sweep_mod.warehouse_records, "list_warehouse_records", lambda *_a, **_k: [])
    monkeypatch.setattr(sweep_mod.trash, "list_all", lambda *_a, **_k: [])
    monkeypatch.setattr(sweep_mod.base_refs, "protected_roots", lambda *_a, **_k: sweep_mod.base_refs.BaseRefs())
    monkeypatch.setattr(sweep_mod, "discover_datasets", lambda *_a, **_k: type("D", (), {"uris": ["s3://b/clone.lance"], "truncated": []})())
    monkeypatch.setattr(
        sweep_mod,
        "compact_one",
        lambda *_a, **_k: DatasetResult(uri="s3://b/clone.lance", refused="unsupported manifest feature flags: 16"),
    )

    results = sweep_mod.run_sweep(MaintenanceSettings())

    assert len(results) == 1 and results[0].refused, "the harness did not produce the refusal under test"
    assert stamped == [], f"a REFUSED dataset was stamped as maintained, hiding it for the whole interval: {stamped}"
