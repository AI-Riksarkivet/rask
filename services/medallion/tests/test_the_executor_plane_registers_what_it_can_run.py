"""The registry's WRITE half, and the submit path's refusal to run what it cannot.

docs/DECISIONS.md "The compute plane is decoupled" (§7.4) step 1. The catalog refuses a transform naming an unregistered
task — which is only a real gate if something actually registers. The plane that submits to Ray is
the plane that says what Ray can run here, and it stamps its own engine rather than trusting a chart
row to spell it: a typo in `engine` would register a task no submitter answers to, producing a
declaration that validates at the door and resolves to nothing at submit.

The mirror property is asserted too. A registration for ANOTHER engine is not an error in the
declaration — it is a transform that belongs to a different executor — and handing its command to
the Ray Jobs API would submit a string written for something else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medallion.core.config import MedallionSettings
from medallion.services.task_register import RAY_ENGINE, register_ray_tasks
from medallion.services.transform_spec import UnrunnableTaskError, resolve_task
from service_kit.lakehouse import task_registry
from service_kit.lakehouse.task_registry import TaskRegistration


def _settings(tmp_path: Path, **over: object) -> MedallionSettings:
    base: dict[str, object] = {
        "control_root": str(tmp_path),
        "ray_code_version": "main-abc1234",
        "ray_tasks": [
            {"task": "stage-transform", "command": "python /home/ray/jobs/ray_stage_job.py", "cardinalities": ["1:1", "1:N"]},
            {"task": "dummy-lane", "command": "python /home/ray/jobs/ray_dummy_job.py", "cardinalities": ["1:1"]},
        ],
    }
    return MedallionSettings.model_validate(base | over)


def test_the_chart_declaration_lands_in_the_registry(tmp_path: Path) -> None:
    """The write, read back the way the catalog reads it — a different process, nothing shared but
    the control root."""
    assert register_ray_tasks(_settings(tmp_path)) == 2

    stored = task_registry.get_task(str(tmp_path), {}, "dummy-lane")

    assert stored is not None
    assert stored.command == "python /home/ray/jobs/ray_dummy_job.py"
    assert stored.cardinalities == ["1:1"]


def test_the_ENGINE_is_stamped_by_the_plane_not_supplied_by_the_chart(tmp_path: Path) -> None:
    """A chart row cannot name an engine, so it cannot name the wrong one.

    The registering plane knows what it submits to; a values file does not, and a typo there would
    survive every test that only checks the record round-trips.
    """
    register_ray_tasks(_settings(tmp_path))

    stored = task_registry.get_task(str(tmp_path), {}, "stage-transform")

    assert stored is not None and stored.engine == RAY_ENGINE


def test_a_task_without_its_own_build_stamp_inherits_the_planes(tmp_path: Path) -> None:
    """One build stamp governs both halves, so a stale registration is detectable against the image
    the submitter is actually running."""
    register_ray_tasks(_settings(tmp_path))

    stored = task_registry.get_task(str(tmp_path), {}, "stage-transform")

    assert stored is not None and stored.code_version == "main-abc1234"


def test_declaring_no_tasks_writes_NOTHING(tmp_path: Path) -> None:
    """An estate that declares no transforms needs no registry, and boot must not manufacture one."""
    assert register_ray_tasks(_settings(tmp_path, ray_tasks=[])) == 0
    assert task_registry.get_task(str(tmp_path), {}, "stage-transform") is None


def test_an_unwritable_control_root_is_LOGGED_and_not_FATAL(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Registration runs in the cascade head's boot. Crashing there would take `/produce` — the whole
    ingest surface — down over a capability nothing has asked for yet, and the consequence is already
    legible: the door answers 422 naming the exact task that is missing.
    """
    with caplog.at_level("ERROR"):
        assert register_ray_tasks(_settings(tmp_path, control_root="")) == 0

    assert "ray_tasks_unregisterable" in caplog.text


# --- the submit path's half -----------------------------------------------------------------------


def test_an_UNREGISTERED_task_is_refused_before_anything_is_submitted(tmp_path: Path) -> None:
    """Refusing beats submitting the key as a command: the second reaches the cluster and dies there,
    naming an image rather than the key nobody registered."""
    with pytest.raises(UnrunnableTaskError, match="nobody-registered-this"):
        resolve_task(_settings(tmp_path), task="nobody-registered-this", engine=RAY_ENGINE)


def test_a_task_registered_for_ANOTHER_engine_is_refused_HERE(tmp_path: Path) -> None:
    """Not a broken declaration — a valid one belonging to a different executor.

    This is the property that makes a second engine possible at all: the same `_tasks/` prefix holds
    both, the catalog validates against both, and each submitter runs only its own.
    """
    task_registry.put_task(str(tmp_path), {}, TaskRegistration(task="compact", engine="inprocess", command="maintenance.compaction"))

    with pytest.raises(UnrunnableTaskError, match="inprocess"):
        resolve_task(_settings(tmp_path), task="compact", engine=RAY_ENGINE)


def test_a_registered_task_resolves_to_its_command(tmp_path: Path) -> None:
    """The happy path, and the reason the submit path reads the registry at all: what a transform
    RUNS comes from the plane that registered it, never from the transform record."""
    register_ray_tasks(_settings(tmp_path))

    assert resolve_task(_settings(tmp_path), task="stage-transform", engine=RAY_ENGINE).command == "python /home/ray/jobs/ray_stage_job.py"
