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
