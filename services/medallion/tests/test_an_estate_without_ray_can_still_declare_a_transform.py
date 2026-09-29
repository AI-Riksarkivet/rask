"""The in-process engine is a real second engine, so it must register what it can run.

`task_register` says the rule outright — "A second executor registers its own tasks under the same
prefix, and the catalog changes not at all" — and `engine_choice` calls the in-process engine "a real
second engine, not a fallback: it is what an estate without a Ray cluster runs on". Both were true of
the reading path and false of the writing one: `put_task` had exactly ONE production caller, which
stamped `engine="ray"` on every record it wrote and read a list the chart rendered only under
`.Values.medallion.ray`.

THE CONSEQUENCE, and it is a catalog door failing for a compute reason. With no Ray, `_tasks/` is
empty, so `POST /v1/project/{id}/transform/set` answers 422 `_unregistered_task` for EVERY declaration
and `engine_for(spec=declared)` raises `UnrunnableTaskError` for the same reason. The only lane that
still ran in-process was the UNDECLARED chart-flag fallback — so an estate that brought its own engine
could run the cascade only by declaring nothing, which is the opposite of BYO.

WHY NOT THE OTHER FIX: making `engine_for` fall back to in-process when no registration exists would
reintroduce running a task on "whichever engine happens to be configured here", which that function
refuses in those words and deliberately. The plane registers what it hosts; nothing else changes.
"""

from __future__ import annotations

from pathlib import Path

from medallion.core.config import MedallionSettings
from medallion.services.engine_choice import IN_PROCESS_ENGINE
from medallion.services.task_register import register_tasks
from service_kit.lakehouse import task_registry


def test_both_planes_register_when_both_are_hosted(tmp_path: Path) -> None:
    """An estate running Ray AND the in-process engine registers both vocabularies, each with its own.

    This is the case that proves the registry is engine-PLURAL rather than merely engine-swapped: the
    catalog resolves each declared task to the plane that actually hosts it.
    """
    settings = MedallionSettings.model_validate(
        {
            "control_root": str(tmp_path),
            # NOT `ray_enabled`: registration follows the LISTS, because each plane declares what it
            # hosts. (The flag additionally requires `compute_enabled`, and demanding it here would
            # couple this property to a config rule it does not depend on.)
            "ray_code_version": "main-abc1234",
            "ray_tasks": [{"task": "heavy-lane", "command": "python /home/ray/jobs/ray_stage_job.py"}],
            "inprocess_tasks": [{"task": "light-lane", "command": "medallion.transform_stage"}],
        }
    )

    assert register_tasks(settings) == 2

    heavy = task_registry.get_task(str(tmp_path), {}, "heavy-lane")
    light = task_registry.get_task(str(tmp_path), {}, "light-lane")
    assert heavy is not None and light is not None
    assert heavy.engine != light.engine, "two planes, two engines — the registry must keep them apart"
    assert light.engine == IN_PROCESS_ENGINE
