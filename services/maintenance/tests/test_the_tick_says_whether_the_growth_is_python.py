"""The tick reports its Python heap blocks, which separate a retained object from a native buffer.

WHY THIS AND NOT A PROFILER. [[LH-183]] has a measured series and an eliminated suspect: across seven
ticks on the deployed estate the Lance session held at 14.6 MB (7.1% of its 204.8 MB cap) for five
consecutive ticks while RSS went 192 -> 251Mi. So something retains and it is not the session cache.
The next question has exactly two answers and they want different fixes:

* **Python objects retained between passes** — the per-tick `DatasetResult` set, the four side maps
  `summarize` builds (`refusals`, `trashed_datasets`, `index_findings`, `errors`), or anything holding
  a reference to them. The fix is to stop retaining, and CPython's allocator counts them.
* **Native allocation Lance or pyarrow does not charge to the session** — buffers behind the 585 dataset
  opens a tick. They are outside CPython's allocator entirely, and the fix is somewhere else.

`sys.getallocatedblocks()` tells them apart in O(1): tracking RSS means Python, flat while RSS climbs
means native.

THE FIRST IMPLEMENTATION USED `len(gc.get_objects())` AND THIS FILE REJECTED IT, which is the reason the
responsiveness leg below exists. Measured on the CPython 3.13.12 in this image, `gc.is_tracked({"n": 1})`
is **False** — a dict whose keys and values are all atomic is untracked — so holding 50,000 of them moved
`gc.get_objects()` by **-188** while `getallocatedblocks()` moved by **+149,728** and returned to baseline
on release. The gc reading would have reported "flat", which reads as "native", which is a confident
wrong answer to the only question this instrument exists to settle.

A COUNT OF BLOCKS, NOT BYTES. Bytes are what RSS already reports; the measurement is the two series
moving together or not, never either number alone.
"""

from __future__ import annotations

from maintenance.services.sweep import summarize


def test_the_tick_summary_carries_the_python_heap_reading() -> None:
    """RED before the fix: the summary reported bytes held by Lance and nothing about the Python heap."""
    summary = summarize([])

    assert "python_blocks" in summary, "a tick that cannot say what Python holds cannot rule Python out"


def test_the_count_is_a_live_reading() -> None:
    """Never a constant or a zero — a field that always answers the same thing measures nothing.

    The bar is the type and a floor rather than a threshold: a CPython process holds tens of thousands of
    allocator blocks before any of this estate's code runs, so anything at or below a few hundred means
    the call is not reaching the allocator.
    """
    value = summarize([])["python_blocks"]

    assert isinstance(value, int), f"expected an int block count, got {type(value).__name__}"
    assert value > 1000, f"a live interpreter holds far more than {value} blocks; this is not a real reading"


def test_two_readings_move_with_the_heap() -> None:
    """The reading must RESPOND, or it cannot separate retention from native allocation.

    Holding 50,000 dicts between two calls has to raise the number. This leg is not ceremony: it FAILED
    the first implementation and sent it back (see the module docstring), and it guards the same failure
    `compaction_mode` already cost this estate once — a field that always answers the same thing, read
    as a measurement (see `docs/DECISIONS.md`).
    """
    before = summarize([])["python_blocks"]
    retained = [{"n": i} for i in range(50_000)]
    after = summarize([])["python_blocks"]

    assert after > before, f"the reading did not move ({before} -> {after}) while 50,000 dicts were held"
    assert len(retained) == 50_000, "keep the reference alive until after the second reading"
