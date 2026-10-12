"""The registry's WRITE half, and the submit path's refusal to run what it cannot.

docs/adr/0052-the-compute-plane-is-decoupled-a-port-two-adapters-and-no.md "The compute plane is decoupled" step 1. The catalog refuses a transform naming an unregistered
task — which is only a real gate if something actually registers. The plane that submits to Ray is
the plane that says what Ray can run here, and it stamps its own engine rather than trusting a chart
row to spell it: a typo in `engine` would register a task no submitter answers to, producing a
declaration that validates at the door and resolves to nothing at submit.

Each engine's registration of a task is its own record, so two engines registering the same task both keep
theirs, and a submitter resolves only its own engine's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medallion.core.config import MedallionSettings
from medallion.services.engine_choice import engine_for
from medallion.services.engine_names import IN_PROCESS_ENGINE
from medallion.services.task_register import register_tasks
from medallion.services.transform_spec import UnrunnableTaskError, resolve_task
from service_kit.lakehouse import task_registry
from service_kit.lakehouse.transform_specs import TransformSpec


def _settings(tmp_path: Path, **over: object) -> MedallionSettings:
    base: dict[str, object] = {
        "control_root": str(tmp_path),
        "engines": {
            "ray": {
                "app_id": "compute",
                "code_version": "ray-cluster:abc1234",
                "tasks": [{"task": "stage-transform", "cardinalities": ["1:1", "1:N"]}, {"task": "dummy-lane", "cardinalities": ["1:1"]}],
            }
        },
    }
    return MedallionSettings.model_validate(base | over)


def test_declaring_no_tasks_writes_NOTHING(tmp_path: Path) -> None:
    """An estate that declares no transforms needs no registry, and boot must not manufacture one."""
    assert register_tasks(_settings(tmp_path, engines={})) == 0
    assert task_registry.registrations_of(str(tmp_path), {}, "stage-transform") == []


def test_an_unwritable_control_root_is_LOGGED_and_not_FATAL(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Registration runs in the cascade head's boot. Crashing there would take the head and `/train`
    down over a capability nothing has asked for yet, and the consequence is already
    legible: the door answers 422 naming the exact task that is missing.
    """
    with caplog.at_level("ERROR"):
        assert register_tasks(_settings(tmp_path, control_root="")) == 0

    assert "tasks_unregisterable" in caplog.text


# --- the submit path's half -----------------------------------------------------------------------


def test_an_UNREGISTERED_task_is_refused_before_anything_is_submitted(tmp_path: Path) -> None:
    """Refusing beats submitting the key as a command: the second reaches the cluster and dies there,
    naming an image rather than the key nobody registered."""
    with pytest.raises(UnrunnableTaskError, match="nobody-registered-this"):
        resolve_task(_settings(tmp_path), task="nobody-registered-this", engine="ray")


def test_a_task_both_engines_register_resolves_to_each_engines_own_registration(tmp_path: Path) -> None:
    """Both engines register `stage-transform`, as the chart deploys them, and neither registration overwrites the other:
    each submitter resolves its own (the out-of-process one with no command and the engine's build, the in-process one
    with its module and this lakehouse's build), and a declaration naming an engine runs on that engine (CP-056)."""
    settings = _settings(
        tmp_path,
        code_version="lakehouse:abc1234",
        compute_enabled=True,
        inprocess_tasks=[{"task": "stage-transform", "command": "medallion.transform_stage", "cardinalities": ["1:1", "1:N"]}],
    )
    assert register_tasks(settings) == 3

    ray = resolve_task(settings, task="stage-transform", engine="ray")
    inprocess = resolve_task(settings, task="stage-transform", engine=IN_PROCESS_ENGINE)
    chosen = {engine: engine_for(settings, spec=_declared(engine)) for engine in ("ray", IN_PROCESS_ENGINE)}

    assert (ray.engine, ray.command, ray.code_version) == ("ray", "", "ray-cluster:abc1234")
    assert (inprocess.engine, inprocess.command, inprocess.code_version) == (IN_PROCESS_ENGINE, "medallion.transform_stage", "lakehouse:abc1234")
    assert chosen == {"ray": "ray", IN_PROCESS_ENGINE: IN_PROCESS_ENGINE}


def _declared(engine: str) -> TransformSpec:
    return TransformSpec(name="lane", project="p", from_id="bronze$a", to_id="silver$a", task="stage-transform", engine=engine)
