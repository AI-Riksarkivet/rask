"""Lance's thread pool follows CPU AFFINITY, and the documented override does not move it.

[[LH-172]]. `lance_docs/guide.md:2989-2996` — and the LIVE upstream page, spot-checked 2026-09-16 as
`PROVENANCE.md` requires for the five unverifiable bundles — says the compute pool "is determined by
the number of cores on the machine" and that `LANCE_CPU_THREADS` overrides it. The first half is true
on pylance 11.0.0. The second is not.

MEASURED, varying only CPU affinity and the variable, same dataset, same host:

    visible CPUs  LANCE_CPU_THREADS   threads created by one scan
        4              unset                      11
        8              unset                      16
       64              unset                      71
        4               32                        11     <- the override is ignored
       64                1                        69     <- and ignored in the other direction

So a core-tracking pool exists and the documented lever does not reach it. A `LANCE_CPU_THREADS`-
setting companion to `affordable_cache_bytes` was written, deployed to all four lakehouse pods (each
logged the bound applying) and then REMOVED, because a control that provably does nothing is the
failure this estate keeps finding, not a harmless default.

WHY THE POD SEES 64 AT ALL, which is the fact that makes this a resilience row rather than trivia: a
cgroup CPU *quota* does not reduce visible CPUs. Measured inside the running containers —
`/sys/fs/cgroup/cpu.max` reads `100000 100000` (one CPU) while `nproc` reads 64 — so every lakehouse
process builds a 64-wide pool against a one-CPU budget, and thread count is a MEMORY multiplier too
(`guide.md:3288`: `io_readahead_buffer + num_cpu_threads * batch_size * …`) against the same 512Mi tier
the cache clamp was built for.

WHAT IS LEFT IS A DECISION, NOT AN EDIT, and it is recorded on the row: the lever that demonstrably
works is affinity (`os.sched_setaffinity`, or a `cpuset` on the pod), and pinning a latency-sensitive
service to one core to bound a thread pool is a trade nobody has ruled on.

THIS MODULE PINS THE MEASUREMENT, not a remedy. `cpu_budget_cores` survives because reading the
container's own quota is correct and useful; what it must not do is imply anything was bounded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from service_kit.lakehouse.lance_session import cpu_budget_cores


def _quota(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cpu.max"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_quota_is_read_as_cores(tmp_path: Path) -> None:
    """cgroup v2 states `<quota> <period>` in microseconds; 100000/100000 is one CPU."""
    assert cpu_budget_cores(source=_quota(tmp_path, "100000 100000\n")) == pytest.approx(1.0)
    assert cpu_budget_cores(source=_quota(tmp_path, "250000 100000\n")) == pytest.approx(2.5)


def test_an_unlimited_container_constrains_nothing(tmp_path: Path) -> None:
    """`max` is what an unconstrained cgroup says, and a laptop or CI runner says it too. Answering a
    number there would describe a limit that does not exist."""
    assert cpu_budget_cores(source=_quota(tmp_path, "max 100000\n")) is None
    assert cpu_budget_cores(source=tmp_path / "does-not-exist") is None


def test_a_malformed_quota_is_not_a_number(tmp_path: Path) -> None:
    """A cgroup layout this does not know must read as "nothing constrains it", never as a guess."""
    assert cpu_budget_cores(source=_quota(tmp_path, "not-a-quota\n")) is None


def test_nothing_claims_to_bound_the_pool() -> None:
    """The removed companion must not come back without the measurement above being redone.

    It was written, deployed and observed APPLYING — all four pods logged it — and it still did
    nothing, because the variable it set is ignored. A reader who finds `cpu_budget_cores` and reaches
    for the obvious next function should find this test instead of shipping the same inert control.
    """
    from service_kit.lakehouse import lance_session

    assert not hasattr(lance_session, "bound_lance_thread_pools"), (
        "a thread-pool bound is back; `LANCE_CPU_THREADS` was measured to be ignored on pylance 11.0.0 "
        "(four visible CPUs with the variable set to 32 still built a four-wide pool) — re-measure before restoring it"
    )
