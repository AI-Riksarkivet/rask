"""The sweep's summary carries the memory readings [[LH-183]] turns on.

[[LH-183]] is a row about the maintenance pod's RSS climbing until it is OOMKilled, and its method is one comparison:
``python_blocks`` against RSS. Flat blocks with climbing RSS means native allocation and no heap fix will touch it;
blocks tracking RSS means Python retention. The tick opens one manifest per dataset, the source LH-183 narrowed to
("native buffers behind the 585 dataset opens a tick"), so the readings ride the tick's own summary line: the
comparison needs both series at the SAME instant, and the row records ~10Mi of spread between two samplers reading
the same tick seconds apart.
"""

from __future__ import annotations

import gc

from maintenance.services.sweep import summarize


#: The readings whose whole purpose is to be compared with each other. Named once: a lane that reports
#: a subset reports nothing, because one series alone cannot tell native from Python.
_MEMORY_KEYS = frozenset({"lance_session_bytes", "lance_session_cap_bytes", "python_blocks", "rss_bytes"})


def test_the_summary_reports_every_reading() -> None:
    """A summary carrying a subset of the readings cannot answer the row's comparison."""
    summary = summarize([])
    assert summary.keys() >= _MEMORY_KEYS, f"the summary lost a reading: {sorted(_MEMORY_KEYS - summary.keys())}"


def test_two_readings_move_with_the_heap() -> None:
    """The reading must RESPOND, or it cannot separate retention from native allocation.

    `python_blocks` is `sys.getallocatedblocks()`, a count of blocks rather than bytes: tracking RSS
    means Python retention, flat while RSS climbs means native allocation. `len(gc.get_objects())` cannot
    answer that: measured on CPython 3.13.12, `gc.is_tracked({"n": 1})` is False (a dict of atomic keys
    and values is untracked), so holding 50,000 of them moved `gc.get_objects()` by -188 while
    `getallocatedblocks()` moved by +149,728 and returned to baseline on release. A reading that stays
    flat reads as "native", a confident wrong answer to the only question this instrument settles.
    """
    # The allocation below triggers collections, which free cyclic garbage earlier tests left and can outweigh the 50,000
    # dicts between the two readings. Collected first and paused across them, the readings see only what this test holds.
    gc.collect()
    gc.disable()
    try:
        before = summarize([])["python_blocks"]
        retained = [{"n": i} for i in range(50_000)]
        after = summarize([])["python_blocks"]
    finally:
        gc.enable()

    assert after > before, f"the reading did not move ({before} -> {after}) while 50,000 dicts were held"
    assert len(retained) == 50_000, "keep the reference alive until after the second reading"
