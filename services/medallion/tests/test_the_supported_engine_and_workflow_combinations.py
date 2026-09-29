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

from medallion.core.config import MedallionSettings
from medallion.services import engine_choice


def _settings(tmp_path: Path, *, ray_enabled: bool) -> MedallionSettings:
    return MedallionSettings.model_validate({"control_root": str(tmp_path), "to_namespace": "silver", "compute_enabled": True, "ray_enabled": ray_enabled})


def test_ray_OFF_and_nothing_declared_runs_in_process(tmp_path: Path) -> None:
    assert engine_choice.engine_for(_settings(tmp_path, ray_enabled=False), spec=None) == engine_choice.IN_PROCESS_ENGINE
