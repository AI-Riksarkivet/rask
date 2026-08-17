"""Transform-lane declaration endpoints — the governed door a medallion lane is DECLARED through.

A lane (one bronze->silver edge: read this, run that baked job, write there) used to exist only as
environment on a mover Deployment. Nothing could enumerate the lanes, review one, or gate who adds
one, and a trigger naming an undeclared lane failed at the Ray submit seam with an error that named
the image rather than the key. This door makes a lane a record like every other governed artefact.

Admin-gated on ``project:<id>#can_administer``, checked EXPLICITLY for the same reason the #84
project policies are: ``/v1/project`` is not a router-guarded resource prefix, and ``project``
defines no reader-tier relation — so describe gates at the admin tier too rather than checking a
phantom relation (which 400s → fail-closed 503 for everyone). The bar is right on its own terms as
well: a lane declaration names an entrypoint that will EXECUTE on the shared Ray cluster against the
tenant's data, so declaring one is an administrative act, not a read.

**An unknown lane is 422, naming the key.** Not 404: the caller supplied a lane that does not name
anything declarable, which is a malformed request in exactly the way a bad enum value is, and the
validation shape already renders the offending field. A 404 would read as "this URL is wrong" when
the URL is right and the key inside it is not. Every write decision lands on the #41 audit trail.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from lance_namespace import InvalidInputError
from pydantic import ValidationError

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.identifiers import CONTROL_ID_RE
from catalog.schemas import (
    ProjectTransformsResponse,
    TransformDeleteResponse,
    TransformLaneRequest,
    TransformSpecRequest,
    TransformSpecResponse,
)
from service_kit.control_emit import emit_control
from service_kit.lakehouse import transform_specs
from service_kit.lakehouse.transform_specs import TransformSpec


log = logging.getLogger(__name__)

project_router = APIRouter(prefix="/v1/project", tags=["transform"])
# PLURAL for the collection read, matching the policy split: the singular prefix keeps the spec's
# ``POST /v1/<object>/{id}/<action>`` grammar for set/describe/delete.
projects_router = APIRouter(prefix="/v1/projects", tags=["transform"])


def _validated_project(id: str) -> str:
    if not CONTROL_ID_RE.match(id):
        raise InvalidInputError(f"invalid project id {id!r}: must match {CONTROL_ID_RE.pattern}")
    return id


def _as_request_validation(exc: ValidationError) -> RequestValidationError:
    """Re-raise the spec model's own errors as a request-validation 422.

    The platform-level rules (safe lane key, baked entrypoint, namespaced params) live on
    ``TransformSpec`` so the catalog and the mover share ONE definition — but that means they fire
    when the handler constructs the spec, not when FastAPI parses the body, and a bare pydantic
    ``ValidationError`` escaping a handler is a 500. A malformed declaration is the caller's fault
    and must read as one, so the errors are translated rather than the rules duplicated onto the
    request model (where the two copies would drift, and the mover's copy is the one that matters).

    ``loc`` is prefixed with ``body`` so the field renders as ``body.lane``, matching every other
    422 this service emits.
    """
    return RequestValidationError(
        [
            {
                "type": error.get("type", "value_error"),
                "loc": ("body", *error.get("loc", ())),
                "msg": error.get("msg", "invalid value"),
                "input": error.get("input"),
            }
            for error in exc.errors()
        ]
    )


def _unknown_lane(project: str, lane: str) -> RequestValidationError:
    """The 422 an undeclared lane earns — shaped so the handler renders ``body.lane``.

    Built rather than raised inline so the message is identical everywhere a lane is resolved: the
    operator sees the same sentence whether they typed the key at this door or a mover resolved it
    from a trigger.
    """
    return RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("body", "lane"),
                "msg": f"no transform is declared for lane {lane!r} in project {project!r}; declare it first via POST /v1/project/{project}/transform/set",
                "input": lane,
            }
        ]
    )


@project_router.post("/{id}/transform/set", response_model_exclude_none=True)
async def set_transform(
    id: str,
    body: TransformSpecRequest,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
) -> TransformSpecResponse:
    """Declare (or re-declare) one lane — admin-gated; idempotent.

    The spec model does the platform-level validation: a DNS-safe lane key, string params that
    cannot collide with the ``RASK_PARAM_`` namespace, and an entrypoint that references a script
    BAKED into the image. That last one is why declaration is the right place for this check — Ray
    documents ``runtime_env`` as development-only, and a lane that cannot be declared can never be
    submitted, whereas a submit-time check has to be remembered by every submit path.

    The platform validates SHAPE and never meaning: what a param does belongs to the workload.
    """
    project = _validated_project(id)
    await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")
    # The project comes from the gated PATH, never the body — a body-supplied project would let an
    # admin of one tenant declare a lane into another while passing the gate on their own.
    try:
        spec = TransformSpec.model_validate({**body.model_dump(), "project": project})
    except ValidationError as exc:
        raise _as_request_validation(exc) from None
    await run_in_threadpool(transform_specs.put_spec, settings.registry_root, settings.storage_options(), spec)
    log.info("transform_spec_set", extra={"project": project, "lane": spec.lane, "code_version": spec.code_version})
    await emit_control(
        control,
        action="transform_set",
        object_type="transform",
        object_id=f"{project}/{spec.lane}",
        actor=f"user:{token.sub}" if token else None,
        extra={"from_id": spec.from_id, "to_id": spec.to_id, "code_version": spec.code_version},
    )
    return TransformSpecResponse.model_validate(spec.model_dump())


@project_router.post("/{id}/transform/describe", response_model_exclude_none=True)
async def describe_transform(
    id: str,
    body: TransformLaneRequest,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
) -> TransformSpecResponse:
    """One lane's declaration — admin-gated; **422 naming the key** when the lane is undeclared."""
    project = _validated_project(id)
    await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")
    spec = await run_in_threadpool(transform_specs.get_spec, settings.registry_root, settings.storage_options(), project, body.lane)
    if spec is None:
        raise _unknown_lane(project, body.lane)
    return TransformSpecResponse.model_validate(spec.model_dump())


@project_router.post("/{id}/transform/delete", response_model_exclude_none=True)
async def delete_transform(
    id: str,
    body: TransformLaneRequest,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
) -> TransformDeleteResponse:
    """Undeclare one lane (idempotent) — admin-gated.

    Idempotent rather than 422-on-missing, unlike describe: delete states a desired END state, and a
    caller asking for a lane to be gone is satisfied by it already being gone. Describe asks a
    question that has no answer, which is the case that must be refused.
    """
    project = _validated_project(id)
    await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")
    existed = await run_in_threadpool(transform_specs.delete_spec, settings.registry_root, settings.storage_options(), project, body.lane)
    log.info("transform_spec_deleted", extra={"project": project, "lane": body.lane, "existed": existed})
    await emit_control(
        control,
        action="transform_deleted",
        object_type="transform",
        object_id=f"{project}/{body.lane}",
        actor=f"user:{token.sub}" if token else None,
        extra={"existed": existed},
    )
    return TransformDeleteResponse(status="deleted" if existed else "absent", project=project, lane=body.lane)


@projects_router.get("/{id}/transforms", response_model_exclude_none=True)
async def list_transforms(
    id: str,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
) -> ProjectTransformsResponse:
    """Every lane declared in this project — the answer to "what actually runs here?".

    Admin-gated like the trio: the same information, and splitting the tier would let an admin be
    shown one record and denied the list of all of them.
    """
    project = _validated_project(id)
    await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")
    specs = await run_in_threadpool(transform_specs.list_specs, settings.registry_root, settings.storage_options(), project)
    specs.sort(key=lambda s: s.lane)
    log.info("transform_specs_listed", extra={"project": project, "transforms": len(specs)})
    return ProjectTransformsResponse(
        project=project,
        transforms=[TransformSpecResponse.model_validate(s.model_dump()) for s in specs],
    )


router = APIRouter()
router.include_router(project_router)
router.include_router(projects_router)
