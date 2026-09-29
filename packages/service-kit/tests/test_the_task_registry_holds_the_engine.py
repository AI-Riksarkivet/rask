"""A task is a REGISTERED KEY the platform resolves, not a program path it validates.

docs/DECISIONS.md "The compute plane is decoupled", step 1 of §7.4. The registry is written by the plane that can run
the task and merely consulted by the catalog, so the engine noun never reaches the published OpenAPI
and a second engine needs no catalog change to be declarable.

Two refusals a path-shaped allowlist cannot make, both at the declaration door:

* **registered for an engine this estate runs** — a task naming an engine nobody deployed is refused at
  declaration rather than discovered at submit;
* **supports the declared cardinality** — `stage_stamp.CARDINALITIES` is a closed vocabulary, and a
  1:N transform declared against a task that only honours 1:1 is a data defect a path check cannot see.
"""

from __future__ import annotations

from service_kit.lakehouse.task_registry import TaskRegistration


def _reg(**over: object) -> TaskRegistration:
    base: dict[str, object] = {"task": "stage-transform", "engine": "ray", "command": "python /home/ray/jobs/ray_stage_job.py"}
    base.update(over)
    return TaskRegistration.model_validate(base)


def test_an_empty_cardinality_list_means_ALL() -> None:
    """A task that declares nothing constrains nothing — otherwise every existing registration would
    have to enumerate the vocabulary to keep working."""
    assert _reg().honours("1:N") is True
    assert _reg().honours("1:1") is True
