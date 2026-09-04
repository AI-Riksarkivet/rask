"""A `TransformSpec` names a registered TASK, not a Ray program path.

`open_compute-decoupling.md` §2.1, step 1 of §7.4, and the change that makes clause 1 true: today
A transform's runnable half is a KEY, not a program path. Nothing in this shared library validates
filenames, so the word "Ray", a directory and three filenames reach every API client through the
catalog's published OpenAPI — and no second engine can be declared at all.

THE ALIAS IS A MIGRATION MECHANISM, NOT POLITENESS, for the reason this module already records for the
`lane`->`name` rename: the model is `extra="forbid"`, so an un-aliased rename REFUSES an old record —
and a refused declaration means a mover runs the chart's program while an operator believes the record
governs it. Reading an old record must keep working; writing must produce the new name.

WHAT REPLACES THE PATH CHECK is not "nothing". The registry check is STRICTER — it can ask "registered
for an engine this estate runs" and "honours the declared cardinality", neither of which a substring
match can do. That validation moves to the declaration door, which is the only place that can read the
registry.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from service_kit.lakehouse.transform_specs import TransformSpec


def _spec(**over: object) -> TransformSpec:
    base: dict[str, object] = {
        "name": "dummy",
        "project": "acme",
        "from_id": "bronze$events",
        "to_id": "silver$dummy",
        "task": "stage-transform",
    }
    base.update(over)
    return TransformSpec.model_validate(base)


def test_a_spec_declares_a_task() -> None:
    assert _spec().task == "stage-transform"


def test_the_record_carries_ONLY_the_new_name() -> None:
    """One spelling on disk. A model that also accepted an older one would keep the engine noun alive
    in every record written by anything that still spoke it."""
    assert "task" in _spec().model_dump_json()
    assert "entrypoint" not in _spec().model_dump_json()


def test_the_spec_no_longer_REQUIRES_a_ray_path() -> None:
    """The whole point. A task registered for any engine is declarable; the path check that made this
    Ray-only is gone, replaced at the door by a registry lookup that is strictly stricter."""
    assert _spec(task="inprocess-transform").task == "inprocess-transform"
    assert _spec(task="spark-compact").task == "spark-compact"


def test_an_empty_task_is_still_REFUSED() -> None:
    """Removing the engine noun must not remove the refusal — a declaration naming nothing is the
    typo the door exists to catch."""
    with pytest.raises(ValidationError):
        _spec(task="")


def test_the_module_no_longer_carries_the_ray_literals() -> None:
    """Clause 1, asserted where it can regress: the constants existed in this shared library, which is
    how the catalog came to know what Ray is."""
    import service_kit.lakehouse.transform_specs as module

    for gone in ("BAKED_JOBS_DIR", "BAKED_CLUSTER_JOBS"):
        assert not hasattr(module, gone), f"{gone} still lives in the shared library"
