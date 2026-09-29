"""A deployment may only choose an engine it actually RUNS, and hosting is a deployment fact.

[[LH-147]]. `engine_choice` already refuses "an engine this deployment does not host" — and that guard
could not fire for Ray, because hosting was asserted by a constant
(`HOSTED_ENGINES = frozenset({RAY_ENGINE, IN_PROCESS_ENGINE})`) that contains Ray whether or not this
deployment runs the Ray lane. A control that cannot fire, in the file whose job is choosing.

WHAT IT COSTS IS WORSE THAN AN ERROR. `stage_runner.py` starts the Dapr Workflow runtime only
`if settings.ray_enabled` — "the only lane with a job to wait for" — while `_dispatch_stage_workflow`
builds a `DaprSagaClient`, which only ENQUEUES. So on a Ray-OFF deployment a declared Ray task passed
the check, was scheduled, and was never executed: no failure, no DLQ, no refusal. `stage_runner.py`
names this asymmetry from ingest's first in-cluster deploy — "the engine running in the sidecar and
still could not run a workflow because the APP side was absent — an asymmetry that looks healthy from
every angle except an actual run."

THE ROOT IS ONE FLAG DOING TWO JOBS. `ray_enabled` is both the chart's DEFAULT engine for an estate
that has declared nothing, and whether this deployment HOSTS the Ray runtime. Those are different
questions and only the second may gate a declaration. The decoupling property the estate needs —
a declaration overrides the chart — is kept in the direction that matters (a task registered for
in-process is honoured on a Ray-ON chart); what is refused is a declaration naming an engine whose
runtime this pod never starts.

LATENT WHEN WRITTEN, and stated that way: all four medallion workloads run `MEDALLION_RAY_ENABLED=true`
(re-measured 2026-09-13), so nothing is currently stranded. It bites a Ray-OFF deployment, which is
exactly the configuration condition 3 of the goal says must work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medallion.core.config import MedallionSettings
from medallion.services import engine_choice
from service_kit.lakehouse import task_registry, transform_specs
from service_kit.lakehouse.task_registry import TaskRegistration
from service_kit.lakehouse.transform_specs import TransformSpec


def _settings(tmp_path: Path, **over: object) -> MedallionSettings:
    # `compute_enabled` because the settings refuse `ray_enabled` without it.
    base: dict[str, object] = {"control_root": str(tmp_path), "to_namespace": "silver", "compute_enabled": True}
    return MedallionSettings.model_validate(base | over)


def _declare(tmp_path: Path, *, task: str, engine: str) -> TransformSpec:
    task_registry.put_task(str(tmp_path), {}, TaskRegistration(task=task, engine=engine, command="whatever this engine runs"))
    transform_specs.put_spec(
        str(tmp_path),
        {},
        TransformSpec(name="lane", project="acme", from_id="bronze$events", to_id="silver$features", task=task),
    )
    spec = transform_specs.get_spec(str(tmp_path), {}, "acme", "lane")
    assert spec is not None, "the spec was just written — a None here means the fixture, not the subject, is broken"
    return spec


@pytest.mark.asyncio
async def test_the_ASYNC_door_refuses_identically(tmp_path: Path) -> None:
    """The stage handler calls `engine_for_async`, so a guard only the sync door applies is a guard the
    deployed path never reaches. It delegates today — one line — and this pins that it keeps doing so,
    because "the guard was on the other door" is how the estate loses controls it already wrote."""
    spec = _declare(tmp_path, task="stage-transform", engine="ray")

    with pytest.raises(engine_choice.UnrunnableTaskError, match="MEDALLION_RAY_ENABLED"):
        await engine_choice.engine_for_async(_settings(tmp_path, ray_enabled=False, transform="lane"), spec=spec)
