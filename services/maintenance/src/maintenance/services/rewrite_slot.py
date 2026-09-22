"""How many compaction REWRITES may be resident at once, independent of how many units may run.

MEASURED 2026-09-22, and the two numbers are not the same number — which is the mistake this module
exists to stop repeating. Bounding the shared thread limiter to the memory-safe figure throttled the
WHOLE lane: the sweep plans ~568 units a tick and all but a handful are no-ops that make two HTTP
calls and return, so the lane needs wide concurrency to drain 4.7 units/sec, while a real rewrite
needs narrow concurrency to fit a 4Gi pod. One knob served both and could satisfy neither — at 8
concurrent the lane managed ~0.67 units/sec against the 4.7 it must sustain, and the backlog grew
without bound.

So the threadpool stays sized for THROUGHPUT and this semaphore bounds MEMORY, around the only step
that holds bytes: the rewrite. A no-op unit never acquires it, because it never reaches the rewrite.

THE SIZE COMES FROM A MEASUREMENT. One rewrite bounded at `max_source_bytes` (256 MiB) peaked at
+434 MiB resident — ~1.7x the byte bound — so the worker's limit divided by that product is how many
may overlap. `tests/unit/test_the_worker_can_hold_every_unit_it_admits.py` multiplies it out against
the pod's declared limit and fails the render when the three drift apart.

A `BoundedSemaphore`, not a plain one: releasing more than was acquired is a bug that would silently
widen the bound, and the bounded form raises instead.
"""

from __future__ import annotations

import functools
import itertools
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager


log = logging.getLogger(__name__)


@functools.cache
def _slots(size: int) -> threading.BoundedSemaphore:
    """One semaphore per process, keyed on the size so equal configurations share it.

    Cached for the same reason `lance_session` is: the bound is meaningless unless every rewrite in
    the process contends for the SAME object.
    """
    return threading.BoundedSemaphore(size)


#: Rewrites this PROCESS has committed. The count that predicts the OOM: [[LH-183]] measured ~10-14 MiB
#: retained per PASS — 9.6 / 14.4 / 54.2 MiB across runs of 1, 1 and 4 commits — against peaks of 644,
#: 724 and 677Mi that all released. The floor rises, the ceiling does not, so `passes x ~12 MiB` over
#: the baseline is what a 4Gi pod is spending. Per COMMIT because that is what the cost tracks: two runs
#: over identically-shaped tables differed 4x in retention and 4x in commits.
_committed = itertools.count(1)


def record_committed_rewrite() -> int:
    """Count one committed rewrite and return the running total for this process.

    `itertools.count` rather than a lock-guarded int: `next()` on it is atomic under CPython, and this
    is called from the threadpool where `execute_unit` runs. A miscount would be a diagnostic that
    quietly disagrees with the thing it is diagnosing.
    """
    return next(_committed)


def resident_bytes() -> int:
    """This process's resident set size, from ``/proc/self/status``.

    Beside the pass count on the same line, because the pair is the measurement: `passes x ~12 MiB`
    over the baseline should track this, and a divergence is the interesting signal either way.

    ``VmRSS`` and never ``ru_maxrss``, which is a high-water mark and so cannot show the release that
    [[LH-183]] measured — the peak of a rewrite DOES come back; only the floor rises.

    NEVER RAISES: this is diagnostics riding a compaction commit, so a kernel without procfs costs a
    field rather than the rewrite, and ``-1`` keeps the field's presence a fixed contract.
    """
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) << 10
    except (OSError, IndexError, ValueError):  # pragma: no cover - a platform without procfs
        return -1
    return -1


@contextmanager
def rewrite_slot(size: int) -> Iterator[None]:
    """Hold one of ``size`` rewrite slots for the duration of the block.

    BLOCKS rather than refusing. A rewrite that cannot start yet is not a failure — the unit is
    already delivered and its ack window is running, so waiting is the behaviour that finishes the
    work, while refusing would return it to the queue to be redelivered into the same contention.
    """
    semaphore = _slots(size)
    if not semaphore.acquire(blocking=False):
        log.info("maintenance_rewrite_waiting", extra={"slots": size})
        semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()
