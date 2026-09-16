"""Lance's compute pool must be sized by the container's CPU quota, not the host's core count.

`lance_docs/guide.md:2989-2996`: *"The number of threads in the compute thread pool is determined by
the number of cores on the machine … can be overridden by setting the `LANCE_CPU_THREADS` environment
variable. This is commonly done when running multiple Lance processes on the same machine (e.g when
working with tools like Ray)."*

MEASURED IN THE RUNNING PODS 2026-09-16 — the same host, from inside the container:

    rask-catalog      /sys/fs/cgroup/cpu.max = "100000 100000"  (1 CPU)   nproc = 64
    rask-maintenance  /sys/fs/cgroup/cpu.max = "100000 100000"  (1 CPU)   nproc = 64

So every lakehouse pod runs a 64-thread compute pool on a one-CPU budget: 64x oversubscribed, and
already throttling at idle (maintenance 116 throttled periods of 3,761). Thread count is a MEMORY
multiplier too — `guide.md:3288` gives `io_readahead_buffer + num_cpu_threads * batch_size *
(raw_vector_size + transformed_vector_size)` — so this is the same 512Mi tier the cache clamp was
built for, reached by a different road.

THE SAME SHAPE AS `affordable_cache_bytes`, deliberately: read the container's own limit rather than
configure a literal, because a literal cannot track a chart value that moves. [[LH-096]] solved the
memory half this way and left the thread half unmeasured.

THE IO POOL IS LEFT ALONE, and that is a decision rather than an oversight. The same page says the
cloud-store default of 64 IO threads is "a fairly conservative default and you may need 128 or 256 …
to saturate network bandwidth" — IO threads are not CPU-bound, so shrinking them to the CPU quota
would trade a contention problem for an unmeasured throughput one.

WHAT THESE TESTS DO AND DO NOT ASSERT. They pin the BOUND — that the number handed to Lance comes from
this container's quota, never asks for zero, and never overwrites an operator's choice. They do NOT
assert that Lance acts on it, and [[LH-172]] records why: driven against the installed pylance 11.0.0,
`LANCE_CPU_THREADS` at 1 versus 64 was indistinguishable in wall time, CPU time and thread count across
a filtered scan of 4,000,000 rows. The name is in the shipped binary, so it is read somewhere; no
workload here shows it changing anything. A test that claimed otherwise would be the control-that-
cannot-fire this estate keeps finding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from service_kit.lakehouse.lance_session import bound_lance_thread_pools, cpu_budget_cores


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
    number there would shrink Lance on the one host that has cores to spare."""
    assert cpu_budget_cores(source=_quota(tmp_path, "max 100000\n")) is None
    assert cpu_budget_cores(source=tmp_path / "does-not-exist") is None


def test_the_compute_pool_is_capped_at_the_quota(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    bound_lance_thread_pools(source=_quota(tmp_path, "100000 100000\n"), env=env)

    assert env["LANCE_CPU_THREADS"] == "1", f"a one-CPU pod still asks for {env.get('LANCE_CPU_THREADS')} compute threads"
    assert "LANCE_IO_THREADS" not in env, "the IO pool was resized too — see the module docstring on why that is a different question"


def test_a_fractional_quota_never_asks_for_zero_threads(tmp_path: Path) -> None:
    """`resources.requests.cpu: 50m` is a real value in this chart. Zero threads is not a pool."""
    env: dict[str, str] = {}
    bound_lance_thread_pools(source=_quota(tmp_path, "5000 100000\n"), env=env)

    assert env["LANCE_CPU_THREADS"] == "1"


def test_an_operator_who_set_it_keeps_it(tmp_path: Path) -> None:
    """The variable is Lance's own documented override. A container-derived default must not silently
    replace a number someone chose on purpose."""
    env = {"LANCE_CPU_THREADS": "8"}
    bound_lance_thread_pools(source=_quota(tmp_path, "100000 100000\n"), env=env)

    assert env["LANCE_CPU_THREADS"] == "8"


def test_an_unconstrained_process_is_left_at_lances_own_default(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    bound_lance_thread_pools(source=_quota(tmp_path, "max 100000\n"), env=env)

    assert env == {}, f"an unconstrained process was capped anyway: {env}"


_ENTRYPOINTS = (
    "services/catalog/src/catalog/main.py",
    "services/lineage/src/lineage/main.py",
    "services/medallion/src/medallion/producer.py",
    "services/medallion/src/medallion/stage_runner.py",
    "services/maintenance/src/maintenance/service.py",
)


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_every_lakehouse_entrypoint_bounds_the_pool_before_it_serves(entrypoint: str) -> None:
    """Derived from the list that already calls `instrument_lance_if_available`, because those are
    exactly the processes that open Lance — a sixth one inherits the rule instead of relearning it."""
    body = (Path(__file__).resolve().parents[2] / entrypoint).read_text(encoding="utf-8")

    assert "bound_lance_thread_pools()" in body, (
        f"{entrypoint} instruments Lance but never sizes its compute pool, so it runs one thread per HOST core "
        "against this pod's CPU quota — measured 64 against 1"
    )
