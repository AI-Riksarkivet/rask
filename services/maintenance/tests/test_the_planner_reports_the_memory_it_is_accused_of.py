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

from maintenance.services.sweep import summarize


#: The readings whose whole purpose is to be compared with each other. Named once: a lane that reports
#: a subset reports nothing, because one series alone cannot tell native from Python.
_MEMORY_KEYS = frozenset({"lance_session_bytes", "lance_session_cap_bytes", "python_blocks", "rss_bytes"})


def test_BOTH_lanes_report_the_SAME_readings() -> None:
    """Two summaries that disagree on which readings they carry are two diagnostics, and the row's
    comparison is only meaningful against one. Pinning the SET rather than each key means a reading
    added later cannot land on one lane alone."""
    serial = summarize([])
    assert serial.keys() >= _MEMORY_KEYS, f"the serial lane lost a reading: {sorted(_MEMORY_KEYS - serial.keys())}"
