"""The (engine x workflow-runtime) matrix, written down instead of inferred from two flags.

[[LH-159]]'s last unwritten clause. The BYO ruling names two axes — bring your own WORKFLOW engine,
bring your own COMPUTE engine — and the row records that "the Ray lane runs ONLY through Dapr
Workflow … a genuine limit on requirement 3 and is invisible from the code's shape, because each axis
reads independent while the product of them is not".

IT IS ONE FLAG DOING THREE JOBS, which is exactly why the product is invisible.
`engine_choice.hosted_engines`' own docstring already says `ray_enabled` "is read here for the SECOND
of its two jobs"; measured 2026-09-16 there are three:

  1. `stage_runner.py:91` — starts the Dapr Workflow runtime, "Only started when the Ray lane is on,
     because that is the only lane with a job to wait for";
  2. `producer.py:119` — starts it for `quality_review_enabled OR ray_enabled`;
  3. `engine_choice` — gates `hosted_engines`, and is the chart's default engine when nothing is
     declared.

So the matrix below is not a design; it is a reading of what the flags already produce. A PINNING test
rather than a defect fix, and it is labelled that way on purpose: nothing here is red today. Its value
is that adding or removing a combination silently becomes impossible, which is the thing LH-159 asks
for and the thing a prose matrix in a document could not give.

THE ONE UNSUPPORTED COMBINATION, stated so a reader does not have to derive it: **Ray WITHOUT a
workflow engine is not expressible.** No setting produces the Ray compute engine with the Dapr
Workflow runtime off, because one flag starts both. The converse IS expressible and is honoured —
in-process on a Ray-ON deployment — and that asymmetry is the decoupling working in the direction it
currently works in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medallion.core.config import MedallionSettings
from medallion.services import engine_choice
from service_kit.lakehouse import task_registry, transform_specs
from service_kit.lakehouse.task_registry import TaskRegistration
from service_kit.lakehouse.transform_specs import TransformSpec


def _settings(tmp_path: Path, *, ray_enabled: bool) -> MedallionSettings:
    return MedallionSettings.model_validate({"control_root": str(tmp_path), "to_namespace": "silver", "compute_enabled": True, "ray_enabled": ray_enabled})


def _declared(tmp_path: Path, engine: str) -> TransformSpec:
    task_registry.put_task(str(tmp_path), {}, TaskRegistration(task="t", engine=engine, command=f"{engine}://run"))
    spec = TransformSpec(name="lane", project="acme", from_id="bronze$events", to_id="silver$features", task="t")
    transform_specs.put_spec(str(tmp_path), {}, spec)
    return spec


def test_ray_OFF_and_nothing_declared_runs_in_process(tmp_path: Path) -> None:
    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=False), spec=None) == engine_choice.IN_PROCESS_ENGINE


def test_ray_ON_and_nothing_declared_runs_ray(tmp_path: Path) -> None:
    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=True), spec=None) == engine_choice.RAY_ENGINE


def test_ray_OFF_with_an_in_process_declaration_runs_in_process(tmp_path: Path) -> None:
    spec = _declared(tmp_path, engine_choice.IN_PROCESS_ENGINE)
    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=False), spec=spec) == engine_choice.IN_PROCESS_ENGINE


def test_ray_ON_with_an_in_process_declaration_still_runs_in_process(tmp_path: Path) -> None:
    """THE CELL THAT CARRIES THE DECOUPLING. A declaration overrides the chart in the direction that
    needs no runtime, so a Ray-ON deployment can still run a task its author chose to keep in-process."""
    spec = _declared(tmp_path, engine_choice.IN_PROCESS_ENGINE)
    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=True), spec=spec) == engine_choice.IN_PROCESS_ENGINE


def test_ray_ON_with_a_ray_declaration_runs_ray(tmp_path: Path) -> None:
    spec = _declared(tmp_path, engine_choice.RAY_ENGINE)
    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=True), spec=spec) == engine_choice.RAY_ENGINE


def test_ray_OFF_with_a_ray_declaration_is_REFUSED_and_names_the_lever(tmp_path: Path) -> None:
    """The cell that must never become a silent default. It is refused, and the message distinguishes
    "this build has no adapter" from "this deployment turned it off" — an operator cannot act on the
    first reading if they are given the second."""
    spec = _declared(tmp_path, engine_choice.RAY_ENGINE)

    with pytest.raises(engine_choice.UnrunnableTaskError) as refusal:
        engine_choice.engine_for(_settings(tmp_path, ray_enabled=False), spec=spec)

    assert "MEDALLION_RAY_ENABLED" in str(refusal.value), f"the refusal does not name the lever that would fix it: {refusal.value}"


def test_RAY_WITHOUT_A_WORKFLOW_ENGINE_IS_NOT_EXPRESSIBLE() -> None:
    """The limit [[LH-159]] asks to be written down, asserted off the source rather than described.

    One flag starts the stage runner's Dapr Workflow runtime AND admits Ray to `hosted_engines`, so
    there is no setting that yields Ray-the-compute-engine with the workflow runtime off. If a second
    flag ever splits them, this test fails and the matrix above needs a new row — which is the point.
    """
    runner = (Path(__file__).resolve().parents[1] / "src" / "medallion" / "stage_runner.py").read_text(encoding="utf-8")
    chooser = (Path(__file__).resolve().parents[1] / "src" / "medallion" / "services" / "engine_choice.py").read_text(encoding="utf-8")

    assert "if settings.ray_enabled:" in runner, (
        "the stage runner no longer gates its workflow runtime on ray_enabled — the product of the two axes has changed"
    )
    assert "KNOWN_ENGINES if settings.ray_enabled" in chooser, (
        "hosted_engines no longer keys on ray_enabled — Ray and the workflow runtime may now be independent"
    )
