"""A mover runs the lane that was DECLARED, not the one baked into its Deployment.

The declaration only means something if the submit path reads it. Otherwise `TransformSpec` is a
record an admin edits and a mover ignores — two sources of truth for what runs, with the governed one
losing, which is worse than having only the ungoverned one because it looks governed.

Three behaviours, and the default matters as much as the feature:

* **Lane unset** — byte-for-byte today's behaviour, off the chart's settings. The same stance
  `ray_code_version` takes, and for the same reason: a deployment that has not opted in must be
  unchanged rather than quietly running under a new scheme.
* **Lane declared** — the record's entrypoint, params and code version WIN. That is the whole point:
  an admin changes what a lane runs through the audited door, not by editing a Deployment.
* **Lane named but UNDECLARED** — refuse. Never fall back to the chart entrypoint, because a silent
  fallback is exactly the failure the record exists to eliminate: the mover would run the OLD program
  while the operator believes the declaration governs it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from medallion.services.transform_spec import UndeclaredTransformError, resolve_transform
from service_kit.lakehouse import transform_specs
from service_kit.lakehouse.transform_specs import TransformSpec


def _settings(tmp_path: Path, transform: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(transform=transform, control_root=str(tmp_path), storage_options=lambda: {})


def _declare(tmp_path: Path, **over: object) -> TransformSpec:
    spec = TransformSpec.model_validate(
        {
            "name": "dummy",
            "project": "acme",
            "from_id": "bronze$events",
            "to_id": "silver$dummy",
            "entrypoint": "python /home/ray/jobs/ray_dummy_job.py",
            "params": {"embed_dim": "8"},
            "code_version": "main-abc1234",
        }
        | over
    )
    transform_specs.put_spec(str(tmp_path), {}, spec)
    return spec


def test_an_unset_lane_resolves_to_None_so_the_chart_settings_still_govern(tmp_path: Path) -> None:
    """The opt-in default. An estate that has declared nothing must behave exactly as before."""
    assert resolve_transform(_settings(tmp_path, transform=""), project="acme") is None


def test_a_declared_lane_resolves_to_its_record(tmp_path: Path) -> None:
    _declare(tmp_path)

    spec = resolve_transform(_settings(tmp_path, transform="dummy"), project="acme")

    assert spec is not None
    assert spec.entrypoint == "python /home/ray/jobs/ray_dummy_job.py"
    assert spec.params == {"embed_dim": "8"}
    assert spec.code_version == "main-abc1234"


def test_a_NAMED_but_UNDECLARED_lane_REFUSES_rather_than_falling_back(tmp_path: Path) -> None:
    """The headline. A fallback here would run the chart's program under the declaration's name."""
    with pytest.raises(UndeclaredTransformError) as caught:
        resolve_transform(_settings(tmp_path, transform="dummy"), project="acme")

    assert "dummy" in str(caught.value), "the refusal must name the lane, like the door's 422 does"
    assert "acme" in str(caught.value)


def test_a_lane_declared_for_ANOTHER_project_does_not_resolve(tmp_path: Path) -> None:
    """Lanes are per-tenant. Resolving another project's record would run their program on our data."""
    _declare(tmp_path, project="globex")

    with pytest.raises(UndeclaredTransformError):
        resolve_transform(_settings(tmp_path, transform="dummy"), project="acme")


def test_a_lane_with_no_project_in_scope_REFUSES(tmp_path: Path) -> None:
    """A lane is keyed (project, lane). Without a project there is no record to resolve, and guessing
    a default tenant is how one tenant's transform runs over another's bytes."""
    _declare(tmp_path)

    with pytest.raises(UndeclaredTransformError):
        resolve_transform(_settings(tmp_path, transform="dummy"), project="")


def test_a_lane_with_no_control_root_REFUSES_rather_than_reading_nothing() -> None:
    """An unconfigured control root cannot distinguish "not declared" from "cannot look", and the
    two need opposite answers. Fail naming the knob, exactly as the catalog register seam does."""
    from types import SimpleNamespace

    settings: Any = SimpleNamespace(transform="dummy", control_root="", storage_options=lambda: {})

    with pytest.raises(UndeclaredTransformError, match="MEDALLION_CONTROL_ROOT"):
        resolve_transform(settings, project="acme")
