"""A task is a REGISTERED KEY the platform resolves, not a program path it validates.

docs/DECISIONS.md "The compute plane is decoupled" (§2.2), step 1 of §7.4. The registry is written by the plane that can run
the task and merely consulted by the catalog, so the engine noun never reaches the published OpenAPI
and a second engine needs no catalog change to be declarable.

Two refusals a path-shaped allowlist cannot make, both at the declaration door:

* **registered for an engine this estate runs** — a task naming an engine nobody deployed is refused at
  declaration rather than discovered at submit;
* **supports the declared cardinality** — `stage_stamp.CARDINALITIES` is a closed vocabulary, and a
  1:N transform declared against a task that only honours 1:1 is a data defect a path check cannot see.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from service_kit.lakehouse.task_registry import TaskRegistration, resolve_task


def _reg(**over: object) -> TaskRegistration:
    base: dict[str, object] = {"task": "stage-transform", "engine": "ray", "command": "python /home/ray/jobs/ray_stage_job.py"}
    base.update(over)
    return TaskRegistration.model_validate(base)


def test_the_registration_holds_the_engine_and_the_command() -> None:
    """The engine noun and the program path live HERE, written by the plane that owns them — not in a
    constant the catalog imports."""
    reg = _reg()
    assert reg.engine == "ray"
    assert reg.command.endswith("ray_stage_job.py")


def test_an_unregistered_task_resolves_to_None() -> None:
    """`None` rather than a default, the same contract `get_spec` states: the caller must be able to
    tell "nobody registered this" from "this is configured", so a typo is refused at the door."""
    assert resolve_task({}, "stage-transform") is None


def test_a_registered_task_resolves() -> None:
    assert resolve_task({"stage-transform": _reg()}, "stage-transform") is not None


def test_an_empty_cardinality_list_means_ALL() -> None:
    """A task that declares nothing constrains nothing — otherwise every existing registration would
    have to enumerate the vocabulary to keep working."""
    assert _reg().honours("1:N") is True
    assert _reg().honours("1:1") is True


def test_a_declared_cardinality_list_CONSTRAINS() -> None:
    reg = _reg(cardinalities=["1:1"])
    assert reg.honours("1:1") is True
    assert reg.honours("1:N") is False, "a 1:N transform on a 1:1-only task is a data defect a path check cannot see"


def test_an_unknown_engine_is_refused_at_REGISTRATION() -> None:
    """Refused where it is cheap. A task registered for an engine nobody deployed would otherwise be
    discovered at submit, hours after the declaration that introduced it."""
    with pytest.raises(ValidationError):
        _reg(engine="")


def test_the_record_forbids_unknown_fields() -> None:
    """`extra="forbid"`, like every sibling record: a misspelled field must not be silently dropped
    into a registration that then means something else."""
    with pytest.raises(ValidationError):
        _reg(comand="python /home/ray/jobs/ray_stage_job.py")  # a plausible typo for `command`


def test_obligations_are_CLAIMED_here_and_verified_elsewhere() -> None:
    """A registration states which of O1..O12 the task claims. The claim is not the proof — the
    platform re-derives it from the written dataset (§2.5), which is the difference between a contract
    and a convention."""
    assert _reg(obligations=["O1", "O4"]).obligations == ["O1", "O4"]
