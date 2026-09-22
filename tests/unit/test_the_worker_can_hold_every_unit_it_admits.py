"""The maintenance worker's memory limit must cover every unit it is allowed to run at once.

[[LH-185]]'s pattern, as an invariant rather than an instance: *a door whose cost is a property of the
DATA does not belong in a pod sized for a request*. Three numbers decide whether that holds for the
compaction lane, and until now all three were separately editable with nothing comparing them:

  * `maintenance.dedicatedWorkers.maxConcurrentCompactions` — how many REWRITES may be resident
  * `maintenance.maxSourceBytes`                      — how many bytes ONE unit may pull in
  * `dedicatedWorkers.resources.limits.memory`        — what the pod may hold

IT MULTIPLIES THE COMPACTION BOUND, NOT THE UNIT ONE, and that distinction is the whole reason
this gate is worth having. `maxConcurrentUnits` is a THROUGHPUT bound on anyio's thread limiter; a
no-op unit holds no bytes. Bounding memory with it throttled the entire lane — measured 2026-09-22,
the drain fell to ~0.67 units/sec against the 4.7 it must sustain and `Unprocessed Messages` climbed
past 2,000. Only the rewrite holds bytes, so only the rewrite's bound belongs in this arithmetic.

MEASURED 2026-09-22, which is what turns this from arithmetic into a bound. A table of 240 MiB in 60
fragments was built through the catalog and left for the sweep; `/proc/1/status` inside the worker,
sampled every 2s across the compaction, went

    294Mi -> 365 -> 414 -> 370 -> 323 -> 427 -> 729 -> 380 -> 316 -> 431 -> 317Mi (flat 60s)

One unit bounded at 256 MiB peaked at **+434 MiB** resident — ~1.7x the byte bound, which is the
factor below. The second worker showed the same shape at +128Mi.

WHY A RATIO AND NOT THE RAW FIGURE: `max_source_bytes` is the knob an operator turns, so the ceiling
has to follow it. Sizing against the one observed peak would pin the gate to a 256 MiB configuration
and say nothing about any other.

THE RATIO IS A LOWER BOUND, and the gate is written to be honest about that: the sample interval was
2s, so a sharper peak between samples is possible, and the headroom factor below is what carries that
uncertainty rather than a pretence of precision.
"""

from __future__ import annotations

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, render


#: Resident bytes per unit, as a multiple of `maxSourceBytes`. Measured: 434 MiB peak for a 256 MiB
#: bound. Raise this only against a new measurement, never to make a configuration fit.
RESIDENT_PER_SOURCE_BYTE = 1.7

#: What the process holds with every unit idle — measured on two workers at 294Mi and 315Mi.
BASELINE_MIB = 320

#: The share of the limit this arithmetic may claim. The peak is a lower bound (2s sampling) and Lance's
#: overhead is not linear in every shape, so the gate refuses a configuration that merely *just* fits.
USABLE_FRACTION = 0.75

_SUFFIX = {"Ki": 1 / 1024, "Mi": 1.0, "Gi": 1024.0, "K": 1000 / (1024 * 1024), "M": 1e6 / (1024 * 1024), "G": 1e9 / (1024 * 1024)}


def _mib(quantity: str) -> float:
    """A Kubernetes memory quantity in MiB."""
    text = str(quantity).strip()
    for suffix, factor in _SUFFIX.items():
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    return float(text) / (1024 * 1024)


def _worker() -> dict:
    found = [d for d in render(*DEFAULT_ARGS) if d.get("kind") == "Deployment" and "maintenance-worker" in d["metadata"]["name"]]
    assert found, "no maintenance-worker Deployment rendered"
    return found[0]


def _env() -> dict[str, str]:
    return {e["name"]: str(e.get("value", "")) for c in _worker()["spec"]["template"]["spec"]["containers"] for e in c.get("env", [])}


def test_the_three_numbers_are_all_declared() -> None:
    """A missing one would make the arithmetic below vacuous rather than false."""
    env = _env()
    assert "MAINTENANCE_MAX_CONCURRENT_UNITS" in env, "the worker declares no throughput ceiling"
    assert "MAINTENANCE_MAX_CONCURRENT_COMPACTIONS" in env, "the worker declares no REWRITE ceiling, so nothing bounds resident bytes"
    assert "MAINTENANCE_MAX_SOURCE_BYTES" in env, "the worker declares no per-unit byte bound, so the pod is sized against nothing"
    limits = _worker()["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    assert "memory" in limits, "the maintenance worker names no memory limit"


def test_the_byte_bound_survives_the_render_as_an_INTEGER() -> None:
    """Helm carries values as float64, so a large int renders as `2.68435456e+08` unless it is forced.

    Measured while writing this: that spelling reaches the container, fails the settings' int parse and
    the worker never starts — a chart-side mistake that surfaces as a crash-looping pod.
    """
    raw = _env()["MAINTENANCE_MAX_SOURCE_BYTES"]
    assert raw.isdigit(), f"MAINTENANCE_MAX_SOURCE_BYTES rendered as {raw!r}, which the worker's settings cannot parse"


def test_the_worker_can_hold_every_unit_it_admits() -> None:
    """The invariant: concurrency x per-unit residency + baseline must fit inside the usable limit."""
    env = _env()
    units = int(env["MAINTENANCE_MAX_CONCURRENT_COMPACTIONS"])
    source_mib = int(env["MAINTENANCE_MAX_SOURCE_BYTES"]) / (1024 * 1024)
    limit_mib = _mib(_worker()["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"])

    needed = units * source_mib * RESIDENT_PER_SOURCE_BYTE + BASELINE_MIB
    usable = limit_mib * USABLE_FRACTION

    assert needed <= usable, (
        f"the worker admits {units} concurrent REWRITES of up to {source_mib:.0f} MiB each. At a MEASURED "
        f"{RESIDENT_PER_SOURCE_BYTE}x resident per source byte that is {needed:.0f} MiB against "
        f"{usable:.0f} MiB usable ({limit_mib:.0f} MiB limit x {USABLE_FRACTION}). Lower "
        "`maxConcurrentCompactions` or `maintenance.maxSourceBytes`, or raise the worker's memory limit — "
        "this is the [[LH-183]] OOM arriving by configuration instead of by accident."
    )


@pytest.mark.parametrize("units", [4, 40])
def test_the_gate_REFUSES_the_ceiling_it_replaced(units: int) -> None:
    """A gate that cannot fail is not a gate — so the number this replaced must be refused.

    40 was anyio's default thread limiter, which nobody chose. At the same byte bound it asks for
    ~17 GB resident in a 4Gi pod.
    """
    env = _env()
    source_mib = int(env["MAINTENANCE_MAX_SOURCE_BYTES"]) / (1024 * 1024)
    limit_mib = _mib(_worker()["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"])
    fits = units * source_mib * RESIDENT_PER_SOURCE_BYTE + BASELINE_MIB <= limit_mib * USABLE_FRACTION
    assert fits == (units == 4), f"{units} concurrent units: expected fits={units == 4}, got {fits}"


# --------------------------------------------------------------------------- #
# The OTHER axis: what a pass never gives back ([[LH-183]])
# --------------------------------------------------------------------------- #

#: MiB a committed rewrite leaves RESIDENT FOR GOOD. Measured 2026-09-22 on two worker processes:
#: +9.6 MiB for 1 commit over 15 fragments, +14.4 for 1 over 60, +54.2 for 4 over 60 — 13.6 per
#: commit. The unit of cost is the PASS, not the dataset and not the work item. Raise this only
#: against a new measurement.
RETAINED_PER_PASS_MIB = 14.0


def test_the_worker_retires_before_its_retention_reaches_the_limit() -> None:
    """The second arithmetic, and it is a DIFFERENT question from the one above.

    `maxConcurrentCompactions` bounds what is resident AT ONCE and the peaks measured there all came
    back. This bounds what never comes back: the floor rises ~14 MiB per committed pass and nothing
    lowers it, so a worker that runs long enough reaches its limit with no single pass being large.
    That is the shape of the [[LH-183]] OOM — two allocator fixes moved it 87m -> 442m -> 460m and
    neither stopped it, because the cause is not a peak.
    """
    env = _env()
    after = int(env["MAINTENANCE_RECYCLE_AFTER_PASSES"])
    limit_mib = _mib(_worker()["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"])
    if after == 0:
        pytest.skip("recycling disabled — an estate that has not measured its own retention says 0")

    needed = after * RETAINED_PER_PASS_MIB + BASELINE_MIB
    usable = limit_mib * USABLE_FRACTION
    assert needed <= usable, (
        f"the worker retires after {after} passes, and a pass retains a MEASURED {RETAINED_PER_PASS_MIB} MiB — "
        f"{needed:.0f} MiB against {usable:.0f} MiB usable ({limit_mib:.0f} MiB x {USABLE_FRACTION}). "
        "Lower `recycleAfterPasses` or raise the worker's limit. The peaks are not the problem here; "
        "the floor is."
    )


@pytest.mark.parametrize(("after", "expected_fits"), [(150, True), (200, False), (300, False)])
def test_the_gate_REFUSES_a_budget_that_does_not_fit(after: int, expected_fits: bool) -> None:
    """A gate that cannot fail is not a gate, and this one caught its own author: 200 was the first
    default written here, taken from the row's raw "~300 passes" without applying the 0.75 usable
    fraction its sibling has always applied. It overruns by 48 MiB and is refused."""
    limit_mib = _mib(_worker()["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"])
    fits = after * RETAINED_PER_PASS_MIB + BASELINE_MIB <= limit_mib * USABLE_FRACTION
    assert fits == expected_fits, f"{after} passes: expected fits={expected_fits}, got {fits}"
