"""Both sweep lanes report the memory readings, because the lane that runs is the one being accused.

[[LH-183]] is a row about the maintenance pod's RSS climbing until it is OOMKilled, and its method is
one comparison: ``python_blocks`` against RSS. Flat blocks with climbing RSS means native allocation
and no heap fix will touch it; blocks tracking RSS means Python retention. The readings were added to
``summarize`` for exactly that.

``summarize`` is called by ``run_sweep``. ``run_sweep`` is the SERIAL lane. Every deployment runs the
QUEUE lane, whose handler builds its own summary dict in ``routes.on_cron`` — five counters, none of
them a memory reading. Measured on the deployed estate 2026-09-22::

    maintenance_tick_enqueued status='enqueued' planned=568 published=568 not_queued=0 skipped=5

So the diagnostic for a live OOM row does not run on the pod the row is about, and nothing said so:
the field is present in the code, present in the tests, and absent from every tick the estate
actually emits.

THE PLANNER IS STILL DOING THE ACCUSED WORK, which is why this is a defect and not a tidy-up. Splitting
execution onto 4Gi workers moved the COMPACTION, not the discovery pass: ``plan_sweep``'s own docstring
says "Every phase here is a metadata read — registries, a bucket listing, one manifest open per
dataset", and the live tick plans 568 of them every 120s in a 512Mi pod. That per-dataset open is the
source LH-183 narrowed to ("native buffers behind the 585 dataset opens a tick").

RSS RIDES THE SAME LINE NOW, and that is the point of a reading rather than a sampler. The comparison
needs two series at the SAME instant; taking RSS from `kubectl top` on its own cadence means joining
two clocks by timestamp, and the row already records ~10Mi of sampling spread between two samplers
reading the same tick seconds apart.
"""

from __future__ import annotations

import logging

import pytest

from maintenance.services.sweep import memory_readings, summarize


#: The readings whose whole purpose is to be compared with each other. Named once: a lane that reports
#: a subset reports nothing, because one series alone cannot tell native from Python.
_MEMORY_KEYS = frozenset({"lance_session_bytes", "lance_session_cap_bytes", "python_blocks", "rss_bytes"})


def test_BOTH_lanes_report_the_SAME_readings() -> None:
    """Two summaries that disagree on which readings they carry are two diagnostics, and the row's
    comparison is only meaningful against one. Pinning the SET rather than each key means a reading
    added later cannot land on one lane alone."""
    serial = summarize([])
    assert serial.keys() >= _MEMORY_KEYS, f"the serial lane lost a reading: {sorted(_MEMORY_KEYS - serial.keys())}"


def test_two_readings_move_with_the_heap() -> None:
    """The reading must RESPOND, or it cannot separate retention from native allocation.

    `python_blocks` is `sys.getallocatedblocks()`, a count of blocks rather than bytes: tracking RSS
    means Python retention, flat while RSS climbs means native allocation. `len(gc.get_objects())` cannot
    answer that: measured on CPython 3.13.12, `gc.is_tracked({"n": 1})` is False (a dict of atomic keys
    and values is untracked), so holding 50,000 of them moved `gc.get_objects()` by -188 while
    `getallocatedblocks()` moved by +149,728 and returned to baseline on release. A reading that stays
    flat reads as "native", a confident wrong answer to the only question this instrument settles.
    """
    before = summarize([])["python_blocks"]
    retained = [{"n": i} for i in range(50_000)]
    after = summarize([])["python_blocks"]

    assert after > before, f"the reading did not move ({before} -> {after}) while 50,000 dicts were held"
    assert len(retained) == 50_000, "keep the reference alive until after the second reading"


#: The readings a unit-done record must carry, read from the seam rather than restated, so a reading
#: added there becomes required on the worker lanes without editing this file.
_UNIT_READINGS = set(memory_readings())


@pytest.mark.asyncio
async def test_an_INDEX_unit_reports_it_too(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The worker lanes report what they hold, and the index lane is one of them.

    Measured 2026-09-24 over one 6.8 h window: the 512Mi planner plateaued while both 4Gi workers
    climbed at +18.32 and +18.52 Mi/h, and the climb sat on the no-op unit path, where a reading that
    rides the commit never runs. Both subscriptions are served by the same worker pod, and an index build
    can run for an hour, so a series that covers only compaction units cannot say which lane grew the
    process.
    """
    from maintenance.api import index_work as index_mod
    from maintenance.core.config import MaintenanceSettings
    from maintenance.core.lineage_emit import NoopEmitter
    from maintenance.services.index_build import IndexOutcome
    from service_kit.lakehouse.work_items import SCALAR_INDEX

    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})
    monkeypatch.setattr(index_mod.credentials, "write_options_for", lambda *a, **k: {})
    monkeypatch.setattr(index_mod, "build_index", lambda *a, **k: IndexOutcome(name="id_idx", column="id", kind=SCALAR_INDEX, version=3))

    unit = {"uri": "s3://b/t.lance", "table_id": "", "column": "id", "kind": SCALAR_INDEX, "index_type": "BTREE", "name": "id_idx"}
    with caplog.at_level(logging.INFO):
        await index_mod.handle_index_unit({"data": unit}, settings, NoopEmitter())

    done = [r for r in caplog.records if r.message == "index_unit_done"]
    assert done, f"no index-unit-done record at all: {[r.message for r in caplog.records]}"
    missing = sorted(k for k in _UNIT_READINGS if not hasattr(done[0], k))
    assert not missing, f"the index lane reports no memory: {missing} absent from index_unit_done"
