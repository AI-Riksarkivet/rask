"""Measure, at BUILD time, the thread pools the lakehouse image bounds, under the environment the image ships.

Run as `python /tmp/pool-gate.py` from the lakehouse dockerfile, after the import gate. A non-zero exit fails the build.

**Why this exists** ([[LH-250]]). numpy's OpenBLAS and pyarrow's CPU pool size themselves to the CPUs a process can
see, which a cgroup CPU quota does not reduce, so on a wide node a lakehouse pod runs both node-wide under a 1-CPU
quota. Both read `OMP_NUM_THREADS`, which the image sets beside the reading that sized it
(`.docker/rest-catalog.dockerfile`). The gate measures the pools against the width the lakehouse gives them: an image
that drops the variable fails on any builder, and one that widens it, or a library that stops reading it, fails on a
builder that sees more CPUs than that width. On a builder with no more, both pools are that narrow whatever is set.

It judges the image's environment, not a pod's: a pod spec that sets `OMP_NUM_THREADS`, or the `OPENBLAS_NUM_THREADS`
that OpenBLAS reads first, overrides it, and the chart sets neither. Lance's own pools are not judged here. They size to
the cgroup quota (`lance_docs/PROVENANCE.md` § The thread pools), and a build runs under none.

Reproduce the build's verdict on the host, where the workspace venv carries both libraries:

    OMP_NUM_THREADS=1 .venv/bin/python .docker/pool-gate.py    # passes, as the image does
    .venv/bin/python .docker/pool-gate.py                      # fails: the variable is undeclared
"""

from __future__ import annotations

import importlib
import os
import sys


#: How many threads each pool may run: a lakehouse pod gets one CPU, so a wider pool only contends with Lance's.
WIDTH = 1


def main() -> int:
    """Exit 0 when `OMP_NUM_THREADS` declares a thread count and no pool runs more than `WIDTH` threads, else 1."""
    declared = os.environ.get("OMP_NUM_THREADS", "")
    if not declared.isdecimal() or int(declared) < 1:
        print(f"pool gate FAILED — the image declares no positive thread count in OMP_NUM_THREADS (read {declared!r})", file=sys.stderr)
        return 1
    widths = measure()
    report = ", ".join(f"{pool} {width}" for pool, width in widths.items())
    if any(width > WIDTH for width in widths.values()):
        print(f"pool gate FAILED — a pool runs more than {WIDTH} thread under OMP_NUM_THREADS={declared}: {report}", file=sys.stderr)
        return 1
    print(f"pool gate: {report} (OMP_NUM_THREADS={declared})")
    return 0


def measure() -> dict[str, int]:
    """How many threads each pool runs: OpenBLAS's are the calling thread and the workers it starts as numpy loads it."""
    before = _thread_count()
    # Loaded between the two counts, never at module top: OpenBLAS starts its workers as numpy loads it.
    importlib.import_module("numpy")
    openblas_workers = _thread_count() - before
    import pyarrow

    return {"OpenBLAS": 1 + openblas_workers, "pyarrow CPU pool": pyarrow.cpu_count()}


def _thread_count() -> int:
    return len(os.listdir("/proc/self/task"))


if __name__ == "__main__":
    raise SystemExit(main())
