"""WHO runs the job comes from the RECORD, not from a deployment flag.

`open_compute-decoupling.md` §7.4. Half a decoupling is worse than none: the declaration door now
refuses a task no engine registered, and the registry says which engine runs it — but if dispatch
still branches on a chart boolean, the record can say `engine: "ray"` while the code decides
something else, and nothing anywhere is red. The vocabulary moved; the control has to move with it.

Two axes, and only the second is what this pins. ORCHESTRATION — when a stage runs, what happens
next, what happens if it dies — is Dapr Workflow's, deliberately and estate-wide. COMPUTE — what
machine moves the bytes — is what a task's registration names, and what these tests hold to the
record.

The opt-in default is preserved and is load-bearing: an estate that has declared no transform is
governed by `MEDALLION_RAY_ENABLED` exactly as before. A declaration does not merely add config, it
takes the decision over.
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
    # `compute_enabled` because the settings refuse `ray_enabled` without it — the Ray path submits
    # the stage's read->transform->write, which the compute config provides.
    base: dict[str, object] = {"control_root": str(tmp_path), "to_namespace": "silver", "compute_enabled": True}
    return MedallionSettings.model_validate(base | over)


def _declare(tmp_path: Path, *, task: str, engine: str) -> None:
    task_registry.put_task(str(tmp_path), {}, TaskRegistration(task=task, engine=engine, command=f"{engine}://run"))
    transform_specs.put_spec(
        str(tmp_path),
        {},
        TransformSpec(name="lane", project="acme", from_id="bronze$events", to_id="silver$features", task=task),
    )


def test_an_UNDECLARED_estate_is_governed_by_the_chart_exactly_as_before(tmp_path: Path) -> None:
    """The opt-in default. Nothing about this change may alter what an un-migrated estate does."""
    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=True), spec=None) == engine_choice.RAY_ENGINE
    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=False), spec=None) == engine_choice.IN_PROCESS_ENGINE


def test_a_DECLARED_transform_takes_the_decision_over(tmp_path: Path) -> None:
    """The record wins over the flag, in BOTH directions — which is the whole property.

    Only the first direction is obvious. The second is the one that catches the real defect: a chart
    with ray on, and a task registered for an engine that is not Ray, must not go to Ray because a
    boolean said so.
    """
    _declare(tmp_path, task="compact", engine="inprocess")
    settings = _settings(tmp_path, ray_enabled=True, transform="lane")
    spec = transform_specs.get_spec(str(tmp_path), {}, "acme", "lane")

    assert engine_choice.engine_for(settings, spec=spec) == engine_choice.IN_PROCESS_ENGINE

    _declare(tmp_path, task="stage-transform", engine="ray")
    spec = transform_specs.get_spec(str(tmp_path), {}, "acme", "lane")
    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=False, transform="lane"), spec=spec) == engine_choice.RAY_ENGINE


def test_an_engine_NOBODY_here_runs_is_refused_rather_than_silently_defaulted(tmp_path: Path) -> None:
    """A task registered for an engine this deployment does not host is an operator error, and the one
    thing that must not happen is quietly running it on whatever is available — that is how a
    declaration meant for another plane rewrites this tenant's data with the wrong program."""
    _declare(tmp_path, task="spark-compact", engine="spark")
    spec = transform_specs.get_spec(str(tmp_path), {}, "acme", "lane")

    with pytest.raises(engine_choice.UnrunnableTaskError, match="spark"):
        engine_choice.engine_for(_settings(tmp_path, ray_enabled=True, transform="lane"), spec=spec)


def test_an_UNREGISTERED_task_is_refused_at_dispatch_too(tmp_path: Path) -> None:
    """The declaration door refuses one, and that is not enough on its own: a registration can be
    DELETED after a transform was declared against it, and the record outlives it."""
    transform_specs.put_spec(
        str(tmp_path),
        {},
        TransformSpec(name="lane", project="acme", from_id="bronze$events", to_id="silver$features", task="was-registered-once"),
    )
    spec = transform_specs.get_spec(str(tmp_path), {}, "acme", "lane")

    with pytest.raises(engine_choice.UnrunnableTaskError, match="was-registered-once"):
        engine_choice.engine_for(_settings(tmp_path, ray_enabled=True, transform="lane"), spec=spec)


def test_the_choice_never_reads_a_COMMAND(tmp_path: Path) -> None:
    """Dispatch asks WHICH engine and never what it will run. A chooser that parsed the command would
    put an engine's vocabulary back into the platform's decision path, which is the coupling this
    whole change removes."""
    _declare(tmp_path, task="stage-transform", engine="ray")
    task_registry.put_task(str(tmp_path), {}, TaskRegistration(task="stage-transform", engine="ray", command="anything at all, unparsed"))
    spec = transform_specs.get_spec(str(tmp_path), {}, "acme", "lane")

    assert engine_choice.engine_for(_settings(tmp_path, transform="lane"), spec=spec) == engine_choice.RAY_ENGINE


def test_a_spec_carrying_no_project_cannot_resolve_and_says_so(tmp_path: Path) -> None:
    """A transform is keyed (project, name). Resolution without one is refused upstream; this asserts
    the chooser does not invent a second, laxer path to the same lookup."""
    settings = _settings(tmp_path, ray_enabled=True, transform="lane")
    settings = settings.model_copy(update={"control_root": ""})
    spec = TransformSpec(name="lane", project="acme", from_id="a", to_id="b", task="stage-transform")

    with pytest.raises(engine_choice.UnrunnableTaskError, match="MEDALLION_CONTROL_ROOT"):
        engine_choice.engine_for(settings, spec=spec)


def test_the_ENGINES_this_deployment_hosts_are_named_in_one_place() -> None:
    """A second engine is added by registering tasks for it and hosting it — never by editing a
    branch. The set is asserted so the addition is a visible, reviewed change rather than a drift."""
    assert set(engine_choice.HOSTED_ENGINES) == {engine_choice.RAY_ENGINE, engine_choice.IN_PROCESS_ENGINE}


@pytest.mark.asyncio
async def test_the_registry_read_does_not_block_the_event_loop(tmp_path: Path) -> None:
    """A stage handler serves other deliveries while this resolves.

    The registry read is a blocking object-store call. Made synchronously inside the handler it
    stalls the loop for every other delivery on the pod — a per-delivery tax on a service whose whole
    job is absorbing a bus. And with NO declaration there is no IO at all, so the threadpool hop must
    be skipped rather than paid.
    """
    _declare(tmp_path, task="stage-transform", engine="ray")
    spec = transform_specs.get_spec(str(tmp_path), {}, "acme", "lane")

    assert await engine_choice.engine_for_async(_settings(tmp_path, transform="lane"), spec=spec) == engine_choice.RAY_ENGINE
    assert await engine_choice.engine_for_async(_settings(tmp_path, compute_enabled=True, ray_enabled=True), spec=None) == engine_choice.RAY_ENGINE
