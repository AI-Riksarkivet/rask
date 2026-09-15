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


def test_a_declared_RAY_task_on_a_ray_OFF_deployment_is_REFUSED(tmp_path: Path) -> None:
    """THE DEFECT. Before this, the declaration won and the stage was enqueued onto a runtime that is
    never started — scheduled, never executed, and silent in every direction."""
    spec = _declare(tmp_path, task="stage-transform", engine="ray")

    with pytest.raises(engine_choice.UnrunnableTaskError) as excinfo:
        engine_choice.engine_for(_settings(tmp_path, ray_enabled=False, transform="lane"), spec=spec)

    message = str(excinfo.value)
    assert "ray" in message, f"the refusal must name the engine an operator has to act on: {message}"
    assert "MEDALLION_RAY_ENABLED" in message, (
        f"an engine this deployment has turned OFF is a lever, not a typo — naming only the hosted set leaves the operator guessing: {message}"
    )


def test_the_same_declaration_RUNS_where_the_ray_lane_IS_hosted(tmp_path: Path) -> None:
    """The control that keeps the guard from becoming a refusal of everything. Identical declaration,
    identical registration — only the deployment differs, which is the whole point of the change."""
    spec = _declare(tmp_path, task="stage-transform", engine="ray")

    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=True, transform="lane"), spec=spec) == engine_choice.RAY_ENGINE


def test_a_declared_IN_PROCESS_task_still_overrides_a_ray_ON_chart(tmp_path: Path) -> None:
    """The decoupling property is kept in the direction that carries it: the record beats the flag, so
    a chart with Ray on must not send a task registered for another engine to Ray. Narrowing hosting
    must not cost this — it is the assertion that makes the declaration mean anything."""
    spec = _declare(tmp_path, task="compact", engine="inprocess")

    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=True, transform="lane"), spec=spec) == engine_choice.IN_PROCESS_ENGINE


def test_in_process_is_hosted_on_a_ray_OFF_deployment(tmp_path: Path) -> None:
    """A Ray-OFF estate is not an estate that can run nothing. In-process needs no workflow runtime —
    `transform.py` calls it directly — so turning the Ray lane off must leave the second engine whole."""
    spec = _declare(tmp_path, task="compact", engine="inprocess")

    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=False, transform="lane"), spec=spec) == engine_choice.IN_PROCESS_ENGINE


def test_an_UNDECLARED_estate_is_unchanged_in_both_directions(tmp_path: Path) -> None:
    """The chart path is a different question — the DEFAULT for an estate that declared nothing — and
    this change must not touch it. Pinned here as well as in its own file because narrowing the hosted
    set is exactly the edit that would silently take the un-migrated estate with it."""
    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=True), spec=None) == engine_choice.RAY_ENGINE
    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=False), spec=None) == engine_choice.IN_PROCESS_ENGINE


@pytest.mark.asyncio
async def test_the_ASYNC_door_refuses_identically(tmp_path: Path) -> None:
    """The stage handler calls `engine_for_async`, so a guard only the sync door applies is a guard the
    deployed path never reaches. It delegates today — one line — and this pins that it keeps doing so,
    because "the guard was on the other door" is how the estate loses controls it already wrote."""
    spec = _declare(tmp_path, task="stage-transform", engine="ray")

    with pytest.raises(engine_choice.UnrunnableTaskError, match="MEDALLION_RAY_ENABLED"):
        await engine_choice.engine_for_async(_settings(tmp_path, ray_enabled=False, transform="lane"), spec=spec)


@pytest.mark.parametrize("ray_enabled", [True, False])
def test_what_may_be_CHOSEN_is_always_a_subset_of_what_can_be_RESOLVED(tmp_path: Path, ray_enabled: bool) -> None:
    """The invariant that survives hosting becoming deployment-derived.

    The two sets answer different questions and it was their EQUALITY that was wrong: the registry's
    is "which adapters exist here", a code fact that is rightly constant, while choosing asks "which
    engines does this deployment run". Subset is the property worth holding — an engine that can be
    chosen and not resolved is a stage that dies at submission — and equality made a Ray-OFF
    deployment unrepresentable.
    """
    from medallion.services import ray_submit
    from medallion.services.engine_registry import hosted_engines as resolvable

    # RUNNABLE, not RESOLVABLE. `hosted_engines()` reports only what an `Executor` adapter serves, and
    # Ray has none — the cascade submits it through `ray_submit` (the Ray Jobs API). The `RayJobExecutor`
    # that made the two sets equal had zero production callers and was deleted 2026-09-15 (owner
    # decision), so comparing against adapters alone would now fail for the engine the estate runs most.
    runnable = set(resolvable())
    if callable(getattr(ray_submit, "submit_stage_job", None)):
        runnable.add(engine_choice.RAY_ENGINE)

    choosable = engine_choice.hosted_engines(_settings(tmp_path, ray_enabled=ray_enabled))

    assert choosable <= runnable, f"a stage may choose {sorted(choosable - runnable)} which nothing executes"
    assert engine_choice.IN_PROCESS_ENGINE in choosable, "the in-process engine needs no runtime and is always hosted"
    assert (engine_choice.RAY_ENGINE in choosable) is ray_enabled, "hosting the Ray lane is exactly whether this deployment starts its runtime"
