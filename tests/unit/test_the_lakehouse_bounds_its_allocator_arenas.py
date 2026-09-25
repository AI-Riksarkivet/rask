"""Every lakehouse container bounds glibc's arena count, because the default is sized by the HOST.

THE DEFECT, measured live 2026-09-21 on the deployed estate ([[LH-183]]). `rask-maintenance` was
OOMKilled against its 512Mi limit, and neither suspect held: `sys.getallocatedblocks()` was flat across
seven ticks, and the Lance session held 14.6 MB of its 204.8 MB cap for five consecutive ticks while
RSS went 192 -> 403Mi. Reading `/proc/1/maps` out of the pod and parsing it OUTSIDE the container
named the remainder:

    pod                arenas (60-68MB anon)   stacks (8-9MB)   anon virtual
    catalog                     34                  103            6,835 MB
    media-to-silver             40                  101            6,619 MB
    medallion-producer          41                  109            6,996 MB
    lineage                     42                  110            6,782 MB
    bronze-to-silver            44                  109            6,925 MB
    silver-to-gold              44                  108            6,924 MB
    maintenance                 59                  117            8,797 MB

Those 60-68 MB reservations are glibc secondary arenas -- `HEAP_MAX_SIZE` is 64 MB on 64-bit. glibc
sizes its arena cap from `sysconf(_SC_NPROCESSORS_ONLN)`, the HOST's core count, and a cgroup CPU
*quota* does not reduce visible CPUs: measured in these containers, `nproc` reads **64** while
`cpu.max` reads `100000 100000`, one CPU. The 64-wide thread pools in such a process are not Lance's,
which sizes both its pools to the quota: reproducing that condition on the host with pylance 12.0.0
(`lance_docs/PROVENANCE.md`), numpy's OpenBLAS starts 63 threads and pyarrow's CPU pool is 64 wide.
Arenas are how those threads become resident bytes.

IT IS FRAGMENTATION, NOT A LEAK, which is why no retaining object was ever found. Each arena keeps its
own free lists and is trimmed independently, and memory freed in one is never handed to another, so RSS
settles at the SUM of per-arena high-water marks: it climbs monotonically and plateaus only once every
arena has seen its worst case.

THE LEVER IS PROVEN, NOT ASSUMED, and that distinction is this estate's own: LH-172 removed a
`LANCE_CPU_THREADS` control after measuring that it moved nothing, because "a control that provably
does nothing is the failure this estate keeps finding". So `MALLOC_ARENA_MAX` was measured before it
was shipped -- 64 threads each forcing arena growth, counting 64MB mappings in `/proc/self/maps`:

    Ubuntu glibc 2.39 (host)          unset -> 64 arenas    =2 -> 1    =1 -> 0
    Debian glibc 2.41 (THE IMAGE)     unset -> 65 arenas    =2 -> 1

The second row ran in `localhost:5000/lance-rest-catalog:heap-blocks`, the image these pods run, in a
THROWAWAY pod -- never by exec into the pod under study, which shares its cgroup and corrupts the
reading it is taking.

WHY 2 AND NOT 1. One arena serialises every allocation in a 100+ thread process on the main arena's
lock. Two is the conventional container setting and already collapses 65 reservations to one secondary.

NOT A SECRET, so the never-through-env rule does not reach it: this is a libc tunable with no
confidentiality, read by glibc at startup and by nothing else.
"""

from __future__ import annotations

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, containers, env_of, render


#: The lakehouse container names FOCUS covers. Each was measured above; each runs the same image family
#: on the same 64-core node under a one-CPU quota. `stage-runner` is ONE name borne by three
#: deployments (bronze-to-silver, media-to-silver, silver-to-gold), and the assertion below walks every
#: container bearing it rather than the first, so a per-deployment regression cannot hide behind a
#: sibling that still renders the bound.
LAKEHOUSE = frozenset({"catalog", "lineage", "medallion-producer", "maintenance", "stage-runner"})

#: The ceiling this gate enforces. 2 is what was measured; the bar is `<= 4` so a later tuning pass has
#: room to trade lock contention against fragmentation without editing this file, while a container
#: that silently returns to the host-sized default still fails.
MAX_ARENAS = 4


#: The dedicated maintenance workers are OFF by default, so the default render does not contain them.
#: They run the SAME image and consume the same `maintenance.work.v1` queue as competing consumers —
#: and unlike the planner they are the half that SCALES (`replicas: 2`), so they carry the arena cost
#: per replica. Their template duplicates the planner's env block rather than sharing it, which is
#: exactly how the two drift.
WORKERS_ON: tuple[str, ...] = (
    *DEFAULT_ARGS,
    "--set", "maintenance.dedicatedWorkers.enabled=true",
    "--set-string", "maintenance.workTopic=maintenance.work.v1",
)  # fmt: skip


def _lakehouse_containers() -> list[tuple[str, str, dict]]:
    return [row for row in containers(render(*DEFAULT_ARGS)) if row[1] in LAKEHOUSE]


def test_the_dedicated_workers_carry_the_bound_too() -> None:
    """The planner and its workers are two templates, so a bound added to one can miss the other.

    FOUND 2026-09-21, immediately after shipping the bound to the planner: `maintenance-worker.yaml`
    duplicates the planner's env block instead of sharing it, so the fix landed on one of the pair and
    the default render could not see the gap — the workers are gated off by `dedicatedWorkers.enabled`
    and simply were not there to check. Its own header claims the two are "rendered from the SAME block
    ... so the two cannot drift", and they are not; that is the drift this leg exists to catch.

    The worker's container is ALSO named `maintenance`, so the parametrized assertion above already
    covers it by name once a render contains it. What was missing was the render, not the rule.
    """
    rows = [(where, env_of(c)) for where, name, c in containers(render(*WORKERS_ON)) if name in LAKEHOUSE]
    workers = [(where, env) for where, env in rows if "maintenance-worker" in where]

    assert workers, "the workers-on overlay rendered no maintenance-worker; this leg would check nothing"
    for where, env in workers:
        assert env.get("MALLOC_ARENA_MAX", "").isdigit(), f"{where} carries no arena bound, and it is the half that scales"


def test_the_render_contains_the_lakehouse_at_all() -> None:
    """Without this the assertions below pass by measuring an empty set."""
    found = {name for _, name, _ in _lakehouse_containers()}

    assert found == LAKEHOUSE, f"the render is missing lakehouse containers {sorted(LAKEHOUSE - found)}; this gate would check nothing"


@pytest.mark.parametrize("service", sorted(LAKEHOUSE))
def test_the_container_bounds_its_arena_count(service: str) -> None:
    """RED before the fix: all four rendered no `MALLOC_ARENA_MAX`, and the pods carried none."""
    rows = [(where, env_of(c)) for where, name, c in _lakehouse_containers() if name == service]
    assert rows, f"no rendered container named {service}"

    for where, env in rows:
        assert "MALLOC_ARENA_MAX" in env, (
            f"{where}/{service} sets no MALLOC_ARENA_MAX, so glibc sizes its arena cap from the host's "
            f"cores (64 here) against a one-CPU quota; measured 34-59 live arenas of 64MB each"
        )
        value = env["MALLOC_ARENA_MAX"]
        assert value.isdigit(), f"{where}/{service} sets MALLOC_ARENA_MAX={value!r}, which glibc cannot parse as a count"
        assert 1 <= int(value) <= MAX_ARENAS, f"{where}/{service} sets MALLOC_ARENA_MAX={value}, outside the stated budget of 1..{MAX_ARENAS}"
