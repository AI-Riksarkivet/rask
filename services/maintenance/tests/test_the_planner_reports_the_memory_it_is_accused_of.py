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

from typing import cast

import pytest

from maintenance.api import routes
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import MaintenanceEmitter
from maintenance.services.sweep import memory_readings, summarize


#: The readings whose whole purpose is to be compared with each other. Named once: a lane that reports
#: a subset reports nothing, because one series alone cannot tell native from Python.
_MEMORY_KEYS = frozenset({"lance_session_bytes", "lance_session_cap_bytes", "python_blocks", "rss_bytes"})


class _Publisher:
    async def publish_event(self, **kwargs: object) -> None:
        return None


class _Emitter:
    def emit_maintenance(self, *args: object, **kwargs: object) -> None:
        return None


class _S:
    work_topic = "maintenance.work.v1"
    work_pubsub = "maintenance-pubsub"
    publish_timeout_seconds = 5.0
    delimiter = "$"


def test_the_readings_are_a_set_not_a_scatter() -> None:
    """A guard on the guard: if `memory_readings` stopped answering, every leg below would pass on an
    empty intersection — the vacuous-pass shape this file's own subject matter is about."""
    assert memory_readings().keys() >= _MEMORY_KEYS, memory_readings()


def test_every_reading_is_a_number_even_when_the_source_is_gone() -> None:
    """Presence is a FIXED contract — `-1` for unavailable, never a missing key, so a reader can tell
    "could not measure" from "nobody reported it". `_session_occupancy` already documents this; RSS
    joins it on the same terms."""
    readings = memory_readings()
    assert all(isinstance(readings[k], int) for k in _MEMORY_KEYS), readings


@pytest.mark.asyncio
async def test_the_QUEUE_lane_reports_the_memory_readings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lane every deployment runs. This is the leg that was RED."""
    monkeypatch.setattr(routes, "plan_sweep", lambda settings: ([], []))
    monkeypatch.setattr(routes, "record_run", lambda: None)

    async def _no_units(*args: object, **kwargs: object) -> tuple[int, list[object]]:
        return 0, []

    async def _no_lineage(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(routes, "enqueue_units", _no_units)
    monkeypatch.setattr(routes, "emit_sweep_lineage", _no_lineage)

    summary = await routes.on_cron(cast(MaintenanceSettings, _S()), cast(MaintenanceEmitter, _Emitter()), _Publisher())

    assert summary["status"] == "enqueued", summary
    missing = _MEMORY_KEYS - summary.keys()
    assert not missing, (
        f"the queue lane's tick summary omits {sorted(missing)} — so on the lane every deployment "
        "actually runs, [[LH-183]]'s diagnostic never fires and the planner's memory is unmeasured "
        "while it still opens one manifest per dataset, 568 a tick, in a 512Mi pod"
    )


def test_BOTH_lanes_report_the_SAME_readings() -> None:
    """Two summaries that disagree on which readings they carry are two diagnostics, and the row's
    comparison is only meaningful against one. Pinning the SET rather than each key means a reading
    added later cannot land on one lane alone."""
    serial = summarize([])
    assert serial.keys() >= _MEMORY_KEYS, f"the serial lane lost a reading: {sorted(_MEMORY_KEYS - serial.keys())}"
