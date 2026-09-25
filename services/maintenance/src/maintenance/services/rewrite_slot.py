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
import os
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


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


#: The last value `record_committed_rewrite` handed out. The counter itself is consumed by reading, so
#: the handler deciding whether to retire cannot ask it — calling `next()` to LOOK would advance the
#: number being looked at. A plain int beside it: the assignment is atomic under CPython, and a read
#: that is one pass stale simply retires one unit later.
_last_committed = 0

#: One-way, per process. Every unit finishing after the mark would otherwise re-signal, and a worker
#: that is already draining does not need to be told again.
_retiring = False


def record_committed_rewrite() -> int:
    """Count one committed rewrite and return the running total for this process.

    `itertools.count` rather than a lock-guarded int: `next()` on it is atomic under CPython, and this
    is called from the threadpool where `execute_unit` runs. A miscount would be a diagnostic that
    quietly disagrees with the thing it is diagnosing.
    """
    global _last_committed
    _last_committed = next(_committed)
    return _last_committed


def passes_committed() -> int:
    """How many rewrites this process has committed, WITHOUT advancing the count."""
    return _last_committed


#: cgroup v2, then v1. Module-level so a test can point them somewhere writable.
_CGROUP_V2_MAX = Path("/sys/fs/cgroup/memory.max")
_CGROUP_V1_LIMIT = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")

#: Above this a v1 `limit_in_bytes` is the kernel's "unlimited" sentinel rather than a budget.
_V1_UNLIMITED = 1 << 62


@functools.cache
def container_memory_limit() -> int:
    """This CONTAINER's memory limit in bytes, or -1 when there is none to read.

    THE CONTAINER'S AND NOT THE HOST'S, which is the distinction this row has already been bitten by:
    `MALLOC_ARENA_MAX` sizes glibc's arenas from the host's CPU count and so governed nothing in a
    512Mi pod. A budget read from `/proc/meminfo` would size the worker for a machine nobody runs it
    on.

    -1 FOR ANYTHING UNCERTAIN — no cgroup file, a v2 `max`, a v1 sentinel, an unparsable line. The
    gate below reads that as "cannot say" and stays off, so an unlimited or non-Linux process keeps
    running rather than retiring on its first unit. Cached because a container's limit cannot change
    while it runs.
    """
    try:
        raw = _CGROUP_V2_MAX.read_text(encoding="utf-8").strip()
        return -1 if raw == "max" else int(raw)
    except (OSError, ValueError):
        pass
    try:
        value = int(_CGROUP_V1_LIMIT.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return -1
    return -1 if value >= _V1_UNLIMITED else value


def should_retire_for_memory(rss: int, *, limit: int, fraction: float) -> bool:
    """Has this process spent its RESIDENT budget? ([[LH-183]])

    THE COUNT ABOVE BOUNDS A LANE THIS ESTATE DOES NOT RUN. `should_retire` is keyed on committed
    rewrites, and this lane commits none: `compaction_versions_removed_total` is ZERO against 374
    no-op plans per worker per thirty minutes. Measured live 2026-09-25 on the deployed estate, the
    consequence is visible rather than inferred — both workers at 866 and 861 MiB against a 4Gi limit,
    ZERO restarts across 34.5 hours, and `maintenance_worker_retiring` never once logged. So
    `passes_committed()` is 0 forever there and the retirement has never fired on the lane that grows.

    Both lanes share the resident set, which is also the thing the OOM killer reads, so that is what
    this gates on. The per-lane cost figures stay what they were measured to be and neither has to
    stand in for the other: ~12 MiB per COMMIT where commits happen, ~1.06 KiB per dataset-operation
    where they do not (two independently-scheduled pods agreeing to 1% over ~270,000 operations each).

    THE FRACTION LEAVES ROOM FOR THE PEAK, and that is why it is well under 1. A rewrite's transient
    peak is 400-700 MiB above the floor and comes back every time; retiring at the floor alone would
    still be OOMKilled by the next peak. At the shipped 0.70 a 4Gi worker leaves at ~2.87 GiB, which
    affords the largest measured peak twice over.

    Non-positive is OFF, and so is any reading of -1, on the same terms as `should_retire`: a
    misconfiguration or an unreadable `/proc` fails toward NOT recycling.
    """
    if fraction <= 0 or limit <= 0 or rss <= 0:
        return False
    return rss >= limit * fraction


def retire_this_worker(*, passes: int, reason: str) -> None:
    """Ask this process to finish what it holds and leave, so Kubernetes starts a fresh one.

    SIGTERM TO SELF, never `sys.exit` and never `os._exit`, because the point is to take the SAME
    path a rolling restart takes: `arm_drain_on_sigterm` flips `app.state.shutting_down` so the next
    delivery is answered RETRY instead of started, uvicorn finishes the in-flight response — which is
    the ack for the very unit that tripped the mark — and the container then exits. Any other exit
    abandons that response and loses the ack the pass was counted for.

    ONE-WAY: `_retiring` cannot become false again, so the units still finishing behind this one do
    not each re-signal.

    `reason` IS REQUIRED because two different budgets reach here and the log line is how an operator
    tells a healthy recycle from a leak: `passes` is meaningless on the memory path, where it is 0.
    """
    global _retiring
    if _retiring:
        return
    _retiring = True
    log.warning(
        "maintenance_worker_retiring",
        extra={"passes": passes, "rss_bytes": resident_bytes(), "memory_limit_bytes": container_memory_limit(), "reason": reason},
    )
    os.kill(os.getpid(), signal.SIGTERM)


def should_retire(passes: int, *, after: int) -> bool:
    """Has this process spent its memory budget? ([[LH-183]])

    The retention is in Lance/pyarrow's native allocator and is not this estate's to fix: a pass
    leaves ~10-14 MiB resident for good while its 400-700Mi peak comes back every time. Retiring on a
    count is the standard answer for an allocator that does not give memory back, and it is the only
    one wholly inside this estate's control.

    CHEAP, WHICH IT WAS NOT BELIEVED TO BE. Measured on the live lane 2026-09-22 ([[LH-190]]): a
    worker restart costs pod-restart-time plus ~5s and the units redeliver at once, because NATS sees
    the subscriber's connection drop rather than waiting out the 720s ack timer.

    `>=` rather than `==`: two threads can commit between checks, and a worker that skipped its exact
    number would run on to the OOM this exists to prevent. Non-positive is OFF, so a misconfiguration
    fails toward NOT recycling — the opposite reading turns a typo into a worker that exits after
    every pass, which is indistinguishable from a crash loop.
    """
    return after > 0 and passes >= after


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
