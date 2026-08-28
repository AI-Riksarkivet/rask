"""A Serve replica must tell the scheduler what it actually costs in RAM.

Found by the Ray design-patterns audit (2026-08-28) against ray-project's own
`doc/source/ray-core/patterns/limit-running-tasks.rst`: when tasks or actors are memory-heavy, cap
concurrency with the `memory` resource rather than by hand.

THE HAZARD IS NOT HYPOTHETICAL HERE — `pipeline.py` records it happening: *"Dropped from 6 actors
after chunk 021's OOM cascade — 6 * ~4GB TrOCR-in-RAM saturated host memory and the kernel OOM
killer reaped dashboard_agent, fate-killing the raylet."* The fix applied at the time was to lower a
hand-tuned constant. That leaves the invariant enforced by a comment: the scheduler was told each
replica costs one CPU and zero bytes, so raising `RASK_SERVE_REPLICAS`, co-deploying transcribe with
htrflow, or landing any other workload on the node re-creates the cascade with nothing in Ray able
to refuse the placement.

The number is the one the estate MEASURED (~4 GB of TrOCR held in RAM), not an invented figure —
which is also why this test covers the transcribe deployment only. The Layout/Line actor pools carry
the same shape with no measured heap behind them, and guessing one would be a worse defect than the
missing declaration: a wrong reservation silently caps throughput.
"""

from __future__ import annotations

import os

import pytest


GIB = 1024**3


def _actor_options(deployment: object) -> dict:
    options = getattr(deployment, "ray_actor_options", None)
    assert options is not None, f"{deployment} exposes no ray_actor_options — the introspection this test needs has moved"
    return dict(options)


def test_the_transcribe_replica_reserves_the_ram_it_holds() -> None:
    from runner.transcribe_service import TranscribeService

    options = _actor_options(TranscribeService)
    assert "memory" in options, (
        "TranscribeService declares no `memory` — Ray is told a ~4GB TrOCR replica costs zero bytes, "
        "so nothing stops the placement that produced chunk 021's OOM cascade"
    )
    assert options["memory"] >= 2 * GIB, f"the declared memory ({options['memory']}) is below the measured TrOCR footprint"


def test_the_reservation_is_operator_tunable() -> None:
    """Hosts differ, so the number is a knob — the same shape as RASK_SERVE_REPLICAS/GPU_FRAC, which
    the platform must never own (`CLAUDE.md`: replica counts and GPU fractions are the RUNNER's)."""
    from runner import transcribe_service

    assert "RASK_SERVE_MEMORY_GB" in transcribe_service.__doc__ or hasattr(transcribe_service, "SERVE_MEMORY_BYTES")


@pytest.mark.skipif(os.environ.get("RASK_SERVE_MEMORY_GB") is not None, reason="the env would override the default under test")
def test_the_default_matches_the_measurement() -> None:
    from runner.transcribe_service import SERVE_MEMORY_BYTES

    assert SERVE_MEMORY_BYTES == 4 * GIB, "the default drifted from the ~4GB the OOM cascade measured"
