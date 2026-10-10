"""WHO runs the job comes from the RECORD, not from a deployment flag.

docs/adr/0052-the-compute-plane-is-decoupled-a-port-two-adapters-and-no.md "The compute plane is decoupled" Half a decoupling is worse than none: the declaration door now
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


def test_a_spec_carrying_no_project_cannot_resolve_and_says_so(tmp_path: Path) -> None:
    """A transform is keyed (project, name). Resolution without one is refused upstream; this asserts
    the chooser does not invent a second, laxer path to the same lookup."""
    settings = _settings(tmp_path, ray_enabled=True, transform="lane")
    settings = settings.model_copy(update={"control_root": ""})
    spec = TransformSpec(name="lane", project="acme", from_id="a", to_id="b", task="stage-transform")

    with pytest.raises(engine_choice.UnrunnableTaskError, match="MEDALLION_CONTROL_ROOT"):
        engine_choice.engine_for(settings, spec=spec)
