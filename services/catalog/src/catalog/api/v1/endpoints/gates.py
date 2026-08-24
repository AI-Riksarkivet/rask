"""Quality-gate declaration endpoints — the governed door a project's gate is CONFIGURED through.

The gate decides whether a stage's output may publish: which column identifies a row, which columns
a consumer depends on, and how far a row count may move before a promotion waits for a human. All of
it lived ONLY as environment on a mover Deployment, so moving a threshold meant editing a values file
and running ``helm upgrade`` — an operation nobody could enumerate, review, or be gated on. This door
makes a gate a record like every other governed artefact, exactly as ``transforms`` did for lanes.

Admin-gated on ``project:<id>#can_administer``, checked EXPLICITLY and for the same reasons the lane
door states: ``/v1/project`` is not a router-guarded resource prefix, and ``project`` defines no
reader-tier relation, so describe gates at the admin tier too rather than checking a phantom relation
(which 400s -> fail-closed 503 for everyone). The bar is also right on its own terms: relaxing a
review band decides whether a human sees a promotion at all, which is an administrative act.

**CLEARING IS A DELETE, NOT A ZERO.** ``delete`` removes the record so the chart's settings govern
again; it never writes zeros. A band of 0.0 is a real, meaningful setting (every non-empty delta
breaches), so "make it stop overriding" and "make it maximally strict" must not be the same call.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.identifiers import CONTROL_ID_RE
from catalog.schemas import GateSpecRequest, GateSpecResponse
from service_kit.control_emit import emit_control
from service_kit.lakehouse import gate_specs
from service_kit.lakehouse.gate_specs import GateSpec


log = logging.getLogger(__name__)

project_router = APIRouter(prefix="/v1/project", tags=["gate"])


def _validated_project(id: str) -> str:
    """The project id, shape-checked before it reaches a record path or an FGA object string."""
    project = id.strip()
    if not CONTROL_ID_RE.fullmatch(project):
        raise RequestValidationError([{"type": "value_error", "loc": ("path", "id"), "msg": "not a valid project id", "input": id}])
    return project


def _as_request_validation(exc: ValidationError) -> RequestValidationError:
    """Render a model refusal as the door's own 422, naming the offending field."""
    return RequestValidationError(
        [{"type": "value_error", "loc": ("body", *(str(part) for part in err["loc"])), "msg": err["msg"], "input": err.get("input")} for err in exc.errors()]
    )


@project_router.post("/{id}/gate/set", response_model_exclude_none=True)
async def set_gate(
    id: str,
    body: GateSpecRequest,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
) -> GateSpecResponse:
    """Declare (or re-declare) this project's gate settings — admin-gated; idempotent."""
    project = _validated_project(id)
    await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")
    # The project comes from the gated PATH, never the body — a body-supplied project would let an
    # admin of one tenant rewrite another tenant's gate while passing the check on their own.
    try:
        spec = GateSpec.model_validate({**body.model_dump(), "project": project})
    except ValidationError as exc:
        raise _as_request_validation(exc) from None
    await run_in_threadpool(gate_specs.put_spec, settings.registry_root, settings.storage_options(), spec)
    log.info("gate_spec_set", extra={"project": project, "review_band": spec.review_band, "review_enabled": spec.review_enabled})
    await emit_control(
        control,
        action="gate_set",
        object_type="gate",
        object_id=project,
        actor=f"user:{token.sub}" if token else None,
        extra={"review_band": str(spec.review_band), "review_enabled": str(spec.review_enabled), "key_column": spec.key_column},
    )
    return GateSpecResponse.model_validate(spec.model_dump())


@project_router.post("/{id}/gate/describe", response_model_exclude_none=True)
async def describe_gate(
    id: str,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
) -> GateSpecResponse | None:
    """This project's declared gate, or ``null`` when the chart's settings still govern.

    ``null`` rather than a populated default: a caller must be able to tell "nothing is declared" —
    where a `helm upgrade` is still the only lever — from "declared, and these are the values".
    """
    project = _validated_project(id)
    await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")
    spec = await run_in_threadpool(gate_specs.get_spec, settings.registry_root, settings.storage_options(), project)
    return GateSpecResponse.model_validate(spec.model_dump()) if spec else None


@project_router.post("/{id}/gate/delete", response_model_exclude_none=True)
async def delete_gate(
    id: str,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
) -> dict[str, bool]:
    """Stop overriding — the chart's settings govern again. Never writes zeros; see the module note."""
    project = _validated_project(id)
    await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")
    removed = await run_in_threadpool(gate_specs.delete_spec, settings.registry_root, settings.storage_options(), project)
    log.info("gate_spec_deleted", extra={"project": project, "removed": removed})
    await emit_control(
        control,
        action="gate_deleted",
        object_type="gate",
        object_id=project,
        actor=f"user:{token.sub}" if token else None,
    )
    return {"removed": removed}


# One exported router, like `transforms`: `router.py` mounts `_module.router` for every endpoint
# module, so a module exposing only its prefixed sub-router is silently never mounted.
router = APIRouter()
router.include_router(project_router)
