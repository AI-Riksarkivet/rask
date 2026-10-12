"""The engine matrix, written down instead of inferred from flags ([[LH-159]]).

The BYO ruling names two axes — bring your own WORKFLOW engine, bring your own COMPUTE engine. A stage
runner hosts no workflow runtime at all: a Ray stage is planned and resolved through its outcome door and
the plan sweep (`stage_plans`, CP-029), so **Ray without a workflow engine is the stage lane's only shape**,
and the compute axis below is decided by the chart default and the declaration alone. (The producer hosts one
for the promotion review alone, which decides no stage's engine; a training run is a plan too, `train_plans`.)

Two settings decide it: `MEDALLION_DEFAULT_ENGINE`, the engine when nothing is declared, and `MEDALLION_ENGINES`, which
engines this deployment hosts (`engine_registry.hosted_engines`). A declaration overrides the default in the direction
that needs no runtime — in-process on a deployment whose default is an engine app — which is the decoupling's cell.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medallion.core.config import MedallionSettings
from medallion.services import engine_choice
from medallion.services.engine_names import IN_PROCESS_ENGINE
from service_kit.lakehouse import task_registry, transform_specs
from service_kit.lakehouse.task_registry import TaskRegistration
from service_kit.lakehouse.transform_specs import TransformSpec


def _settings(tmp_path: Path, *, ray_enabled: bool) -> MedallionSettings:
    hosted = {"engines": {"ray": {"app_id": "compute"}}, "default_engine": "ray"} if ray_enabled else {}
    return MedallionSettings.model_validate({"control_root": str(tmp_path), "to_namespace": "silver", "compute_enabled": True} | hosted)


def _declared(tmp_path: Path, engine: str) -> TransformSpec:
    task_registry.put_task(str(tmp_path), {}, TaskRegistration(task="t", engine=engine, command=f"{engine}://run"))
    spec = TransformSpec(name="lane", project="acme", from_id="bronze$events", to_id="silver$features", task="t")
    transform_specs.put_spec(str(tmp_path), {}, spec)
    return spec


@pytest.mark.parametrize(
    ("ray_enabled", "declared", "engine"),
    [
        pytest.param(False, None, IN_PROCESS_ENGINE, id="ray-off-nothing-declared"),
        pytest.param(True, None, "ray", id="ray-on-nothing-declared"),
        # THE CELL THAT CARRIES THE DECOUPLING. A declaration overrides the chart in the direction that
        # needs no runtime, so a Ray-ON deployment can still run a task its author chose to keep in-process.
        pytest.param(True, IN_PROCESS_ENGINE, IN_PROCESS_ENGINE, id="ray-on-in-process-declared"),
        pytest.param(True, "ray", "ray", id="ray-on-ray-declared"),
    ],
)
def test_each_supported_cell_of_the_matrix_runs_its_engine(tmp_path: Path, ray_enabled: bool, declared: str | None, engine: str) -> None:
    spec = _declared(tmp_path, declared) if declared else None
    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=ray_enabled), spec=spec) == engine
