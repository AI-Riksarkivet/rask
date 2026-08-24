"""The transform-spec registry: a lane is a DECLARED record, not a Deployment's env block.

A lane used to exist only as env on a mover pod — `MEDALLION_FROM_URI`, `MEDALLION_RAY_ENTRYPOINT`,
`MEDALLION_RAY_JOB_PARAMS`. That has three defects this registry exists to close:

* **It is not a record.** Nothing can list the lanes, diff them, or answer "what runs on this
  project?" without reading a Deployment. A lane that is a governed artefact can be gated, audited
  and reviewed like every other one.
* **An unknown lane fails DEEP.** With env-only declaration, a trigger naming a lane nobody
  configured reaches the submit seam before anything notices — the failure surfaces as a Ray job
  that will not start, attributed to the image. Declared lanes let the door answer 422 naming the
  key, which is the difference between a typo and an outage.
* **It does not survive the pod.** The whole point of the durability test below: a spec written by
  the catalog must be readable by a mover that has never met it, in a different pod, after a
  restart. Object-store-backed records do that; process memory does not.

Same stateless-over-object-store shape as `maintenance_policies` and the warehouse registry, and for
the same reason: one service WRITES (the catalog, admin-gated) and another READS (the medallion
mover, which holds no catalog client on the submit path). A per-service copy of the format would
drift, so the format lives in service_kit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from service_kit.lakehouse import transform_specs
from service_kit.lakehouse.transform_specs import TransformSpec


def _spec(**overrides: object) -> TransformSpec:
    base: dict[str, object] = {
        "name": "dummy",
        "project": "acme",
        "from_id": "bronze$events",
        "to_id": "silver$dummy",
        "entrypoint": "python /home/ray/jobs/ray_dummy_job.py",
        "params": {"batch_size": "64"},
        "code_version": "main-abc1234",
    }
    return TransformSpec.model_validate(base | overrides)


# --- the record survives the pod ------------------------------------------------------------------


def test_a_written_spec_is_readable_by_a_reader_that_never_saw_the_write(tmp_path: Path) -> None:
    """The durability property, stated as the deploy actually exercises it.

    The catalog pod writes; a mover pod that started later — holding no shared memory, no cache and
    no catalog client — resolves the lane. Reading through a fresh call with only the control root
    is exactly that: nothing but the object store carries the record across.
    """
    root = str(tmp_path)
    transform_specs.put_spec(root, {}, _spec())

    loaded = transform_specs.get_spec(root, {}, "acme", "dummy")

    assert loaded is not None, "the spec did not survive the write — a restarted mover sees no lane"
    assert loaded.entrypoint == "python /home/ray/jobs/ray_dummy_job.py"
    assert loaded.params == {"batch_size": "64"}
    assert loaded.code_version == "main-abc1234"


def test_an_unknown_lane_resolves_to_None_rather_than_a_default(tmp_path: Path) -> None:
    """No implicit lane. A missing declaration must be legible as missing, so the door can 422 it —
    a defaulted lane would run SOMETHING for a name nobody declared."""
    transform_specs.put_spec(str(tmp_path), {}, _spec())

    assert transform_specs.get_spec(str(tmp_path), {}, "acme", "nosuchlane") is None


def test_lanes_are_scoped_per_project(tmp_path: Path) -> None:
    """Two tenants may both declare `dummy`; one must never resolve the other's."""
    root = str(tmp_path)
    transform_specs.put_spec(root, {}, _spec(project="acme", to_id="silver$acme"))
    transform_specs.put_spec(root, {}, _spec(project="globex", to_id="silver$globex"))

    acme = transform_specs.get_spec(root, {}, "acme", "dummy")
    globex = transform_specs.get_spec(root, {}, "globex", "dummy")

    assert acme is not None and globex is not None
    assert (acme.to_id, globex.to_id) == ("silver$acme", "silver$globex")


def test_put_is_idempotent_so_re_declaring_replaces_rather_than_duplicates(tmp_path: Path) -> None:
    root = str(tmp_path)
    transform_specs.put_spec(root, {}, _spec())
    transform_specs.put_spec(root, {}, _spec(code_version="main-def5678"))

    assert len(transform_specs.list_specs(root, {}, "acme")) == 1
    loaded = transform_specs.get_spec(root, {}, "acme", "dummy")
    assert loaded is not None and loaded.code_version == "main-def5678"


def test_delete_is_idempotent(tmp_path: Path) -> None:
    root = str(tmp_path)
    transform_specs.put_spec(root, {}, _spec())

    assert transform_specs.delete_spec(root, {}, "acme", "dummy") is True
    assert transform_specs.delete_spec(root, {}, "acme", "dummy") is False
    assert transform_specs.get_spec(root, {}, "acme", "dummy") is None


def test_listing_is_scoped_and_skips_an_unreadable_record(tmp_path: Path) -> None:
    """One corrupt record must never void the others — a lane listing that silently emptied would
    read as "this project declares nothing" while its lanes keep running."""
    root = str(tmp_path)
    transform_specs.put_spec(root, {}, _spec(name="a"))
    transform_specs.put_spec(root, {}, _spec(name="b"))
    transform_specs.put_spec(root, {}, _spec(project="other", name="c"))
    corrupt = tmp_path / transform_specs.SPECS_PREFIX / "acme-corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")

    listed = transform_specs.list_specs(root, {}, "acme")

    assert sorted(s.name for s in listed) == ["a", "b"]


# --- the platform-level invariants a declaration must satisfy --------------------------------------


def test_a_runtime_env_style_entrypoint_is_REFUSED_at_declaration() -> None:
    """B3/production-way, enforced where it can be enforced ONCE.

    Ray documents `runtime_env` as a development convenience; the estate's rule is that a lane runs
    from a path baked into the image. Checking that at submit time means every submit path has to
    remember; checking it at DECLARATION means an undeclarable lane can never be submitted at all.
    """
    with pytest.raises(ValidationError, match="baked"):
        _spec(entrypoint="python my_local_script.py")


def test_an_entrypoint_outside_the_baked_jobs_directory_is_REFUSED() -> None:
    with pytest.raises(ValidationError, match="baked"):
        _spec(entrypoint="python /tmp/uploaded_job.py")


@pytest.mark.parametrize("lane", ["", "Has Space", "../escape", "UPPER", "a" * 65])
def test_an_unsafe_lane_key_is_REFUSED(lane: str) -> None:
    """The lane becomes an object-store key and an FGA-adjacent identifier; a traversing or
    shell-shaped name must never reach either."""
    with pytest.raises(ValidationError):
        _spec(name=lane)


def test_params_are_strings_because_the_platform_forwards_them_as_env() -> None:
    """The platform never interprets a param — but it does have to put it in an env var, so a
    non-string is refused at the door rather than str()'d into something the workload misreads."""
    with pytest.raises(ValidationError):
        TransformSpec.model_validate(
            {
                "name": "dummy",
                "project": "acme",
                "from_id": "bronze$events",
                "to_id": "silver$dummy",
                "entrypoint": "python /home/ray/jobs/ray_dummy_job.py",
                "params": {"batch_size": 64},
                "code_version": "main-abc1234",
            }
        )


def test_a_param_key_that_would_collide_with_a_platform_variable_is_REFUSED() -> None:
    """The `RASK_PARAM_` prefix already namespaces these on the wire. Refusing a key that carries
    the prefix ITSELF keeps that guarantee from being unwound by a lane declaring
    `RASK_PARAM_S3_SECRET` and landing `RASK_PARAM_RASK_PARAM_S3_SECRET`... or, worse, a future
    submit path that forgets to re-prefix."""
    with pytest.raises(ValidationError):
        _spec(params={"RASK_PARAM_X": "1"})
