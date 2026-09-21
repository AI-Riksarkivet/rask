"""`MALLOC_ARENA_MAX` only governs glibc, so Arrow must allocate through glibc ([[LH-183]]).

THE ROW'S OWN HISTORY IS THE ARGUMENT. Bounding glibc arenas drove the arena count 60 -> 0 on the
maintenance worker and changed the RSS trend by nothing: the pod still died `OOMKilled, exit 137`,
just later — 442m35s against a pre-fix 86m52s. Measured on the live process afterwards, two allocators
besides glibc are resident and neither is reachable by that tunable:

* **mimalloc**, via pyarrow — `pa.default_memory_pool().backend_name` is `mimalloc` on pyarrow 25.0.0,
  and `arrow::mimalloc_memory_pool` is a symbol in the shipped `libarrow.so.2500`;
* **jemalloc**, via duckdb — 76 jemalloc references in its extension module and a live
  `jemalloc_bg_thd` thread in the running process.

So the bound was not too loose. It was bounding an allocator the service barely uses.

`ARROW_DEFAULT_MEMORY_POOL=system` routes Arrow's allocations through plain `malloc`, which the
`MALLOC_ARENA_MAX` already in the chart then DOES govern. That is why this is one variable rather than
a new lever: it makes the existing lever reach the allocator doing the work. The name was verified
against the shipped `libarrow.so.2500` with `strings` rather than taken from memory — a library that
does not read the variable would leave this gate asserting about nothing.

BOTH NAMES OR NEITHER, which is what this gate is for: shipping the arena bound without the pool
routing is the configuration that has already been measured not to work, and it looks correct.
"""

from __future__ import annotations

from chart_render import DEFAULT_ARGS, containers, env_of, render


#: The containers that run the lakehouse's Python services and therefore allocate through Arrow.
LAKEHOUSE = frozenset({"catalog", "lineage", "medallion-producer", "maintenance", "stage-runner"})

#: The overlay that renders the DEDICATED maintenance workers — the pod class that actually died.
#: Spelled as `test_the_lakehouse_bounds_its_allocator_arenas` spells it, because a near-miss toggle
#: renders no worker at all and the leg below would then assert over an empty list.
WORKERS_ON: tuple[str, ...] = (
    *DEFAULT_ARGS,
    "--set", "maintenance.dedicatedWorkers.enabled=true",
    "--set-string", "maintenance.workTopic=maintenance.work.v1",
)  # fmt: skip


def _lakehouse_containers(*args: str) -> list[tuple[str, dict[str, str]]]:
    rows = containers(render(*(args or DEFAULT_ARGS)))
    return [(where, env_of(c)) for where, name, c in rows if name in LAKEHOUSE]


def test_the_walk_finds_the_lakehouse() -> None:
    """Without this every assertion below could hold over an empty set."""
    assert len(_lakehouse_containers()) >= 5, f"only {len(_lakehouse_containers())} lakehouse containers rendered"


def test_every_lakehouse_container_routes_ARROW_through_the_bounded_allocator() -> None:
    """The half that was missing while the pod kept dying."""
    missing = [name for name, env in _lakehouse_containers() if env.get("ARROW_DEFAULT_MEMORY_POOL") != "system"]

    assert not missing, (
        "these containers bound glibc's arenas while Arrow allocates through mimalloc, which that bound "
        f"cannot reach — the configuration measured to die at 442m: {missing}"
    )


def test_the_two_names_ship_TOGETHER() -> None:
    """Either alone is the configuration already measured not to work, and it looks correct."""
    half = [name for name, env in _lakehouse_containers() if bool(env.get("MALLOC_ARENA_MAX")) != bool(env.get("ARROW_DEFAULT_MEMORY_POOL"))]

    assert not half, f"these carry one of the pair and not the other, which is the shape that fooled this row once: {half}"


def test_the_dedicated_maintenance_WORKERS_carry_it_too() -> None:
    """The worker is the pod that actually died; an overlay that misses it fixes the wrong process."""
    missing = [name for name, env in _lakehouse_containers(*WORKERS_ON) if not env.get("ARROW_DEFAULT_MEMORY_POOL")]

    assert not missing, f"the dedicated maintenance workers allocate through an unbounded pool: {missing}"
