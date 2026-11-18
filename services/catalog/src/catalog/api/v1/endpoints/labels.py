"""Label doors: the labels a project defines, and their attachment to its tables and namespaces (ADR 0211).

Every door is rask's own, so it mounts at ``/management/v1`` and inherits the three rungs `api/v1/router.py` gives
every route.

- **On a table or namespace**, addressed by name as every governed door is and keyed by the object's uuid:
  ``…/labels/describe`` is a metadata read, and the router asks ``can_get_metadata``; ``…/labels/set`` and
  ``…/labels/remove`` change how the object is classified, and the router asks ``can_label``, the owner's act, as
  arming protection is (`catalog.api.fga_deps`). Each change is announced as an untargeted control event.
- **On a project**, which the router does not guard, so each door gates itself as the project's other doors do: the
  definitions and the labelled objects are read by a member of the project or the estate admin, and anyone else is
  answered the missing project's 404; a definition is made, evolved and deleted by the project's admin
  (``can_administer``).

A label authorizes nothing: no door reads one to decide whether to answer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Annotated, Final

from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
    InvalidInputError,
    LanceNamespace,
    NamespaceNotEmptyError,
    NamespaceNotFoundError,
    PermissionDeniedError,
    TableNotFoundError,
)

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, NamespaceDep, ResolvedTableDep, SettingsDep
from catalog.api.security import Caller, CurrentToken
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier
from catalog.schemas import LabelDefinitionRequest, LabelledObjectsResponse, ObjectLabelsResponse, RemoveLabelRequest, SetLabelRequest
from catalog.services import object_labels, table_claims, warehouses
from catalog.services import projects as project_registry
from catalog.services.object_labels import Attachment, LabelDefinition, LabelScope
from lakehouse import records, trash
from lakehouse.control_emit import ControlEmitter, emit_control
from lakehouse.control_events import ControlAction
from lakehouse.governed import fga
from lakehouse.naming import CONTROL_ID_RE


log = logging.getLogger(__name__)

table_router = APIRouter(prefix="/management/v1/table", tags=["labels"])
namespace_router = APIRouter(prefix="/management/v1/namespace", tags=["labels"])
projects_router = APIRouter(prefix="/management/v1/projects", tags=["labels"])


def _actor(token: Caller | None) -> str | None:
    return f"user:{token.subject}" if token is not None else None


def _object(kind: LabelScope, uuid: str) -> str:
    """The OpenFGA object the labelled ``kind`` is held by (ADR 0173 §8)."""
    return fga.table_object(uuid) if kind == "table" else fga.namespace_object(uuid)


#: The control actions an attachment and a removal announce, by the kind of object labelled.
_LABELLED: Final[dict[LabelScope, tuple[ControlAction, ControlAction]]] = {
    "table": ("table_labelled", "table_unlabelled"),
    "namespace": ("namespace_labelled", "namespace_unlabelled"),
}


async def _project_of(settings: Settings, segments: Sequence[str]) -> str:
    """The project the object ``segments`` names lies in: its warehouse's.

    Raises:
        InvalidInputError: The object lies in the estate's own warehouse, which no project owns, so no project's labels
            can reach it.
    """
    project = await run_in_threadpool(warehouses.project_of, settings.registry_root, settings.storage_options(), segments[0])
    if project is None:
        raise InvalidInputError(f"{fga.canonical_object_id(list(segments))} lies in no project's warehouse, so no project's labels apply to it")
    return project


async def _defined(settings: Settings, project: str) -> dict[str, LabelDefinition]:
    return {
        defined.name: defined for defined in await run_in_threadpool(object_labels.definitions, settings.registry_root, settings.storage_options(), project)
    }


async def _describe(settings: Settings, *, kind: LabelScope, segments: Sequence[str], uuid: str) -> ObjectLabelsResponse:
    """The object's labels, and the labels its project defines for its kind, which its owner may attach."""
    attachment = await run_in_threadpool(object_labels.attachment_of, settings.registry_root, settings.storage_options(), kind, uuid)
    defined = await _defined(settings, await _project_of(settings, segments))
    return ObjectLabelsResponse(
        kind=kind,
        id=fga.canonical_object_id(list(segments)),
        uuid=uuid,
        labels=object_labels.labels_shown(attachment, defined),
        available=[d for d in defined.values() if kind in d.scope],
    )


async def _uuid_now(ns: LanceNamespace, settings: Settings, kind: LabelScope, segments: Sequence[str]) -> str | None:
    """The uuid of the object the name holds now, ``None`` when it holds none."""
    try:
        return (await fga_deps.identify(ns, settings, kind, list(segments))).uuid
    except (TableNotFoundError, NamespaceNotFoundError):
        return None


async def _set(
    ns: LanceNamespace,
    settings: Settings,
    control: ControlEmitter,
    token: Caller | None,
    body: SetLabelRequest,
    *,
    kind: LabelScope,
    segments: Sequence[str],
    uuid: str,
    location: str | None,
) -> ObjectLabelsResponse:
    """Attach ``body``'s label to the object, judged against the definition its project holds, and announce it.

    An object's ending ends its labels (`lakehouse.object_records`), so a label attached as its object ended would be a
    record nothing ends: the name is resolved again after the write, and where it no longer holds this object the record
    is taken back and the caller answered as for any object that is gone.
    """
    project = await _project_of(settings, segments)
    so = settings.storage_options()
    defined = await run_in_threadpool(object_labels.definition, settings.registry_root, so, project, object_labels.require_name(body.name))
    if defined is None:
        raise InvalidInputError(f"project {project!r} defines no label {body.name!r}; its admin defines it first")
    value = object_labels.judged_value(defined, kind=kind, value=body.value)
    canonical = fga.canonical_object_id(list(segments))
    base = Attachment(kind=kind, uuid=uuid, id=canonical, project=project, location=location, labels={}, set_by=_actor(token))
    try:
        await run_in_threadpool(object_labels.attach, settings.registry_root, so, base, defined=defined, value=value)
    except records.RecordChangedError as exc:
        raise ConcurrentModificationError(f"the labels of {canonical} kept changing; set the label again") from exc
    if await _uuid_now(ns, settings, kind, segments) != uuid:
        await run_in_threadpool(object_labels.forget, settings.registry_root, so, kind, uuid)
        raise TableNotFoundError(f"{kind} not found: {canonical}")
    await emit_control(
        control,
        action=_LABELLED[kind][0],
        object_type=kind,
        object_id=_object(kind, uuid),
        object_name=canonical,
        actor=_actor(token),
        extra={"label": defined.name, **({"value": value} if value is not None else {})},
    )
    return await _describe(settings, kind=kind, segments=segments, uuid=uuid)


async def _remove(
    settings: Settings, control: ControlEmitter, token: Caller | None, body: RemoveLabelRequest, *, kind: LabelScope, segments: Sequence[str], uuid: str
) -> ObjectLabelsResponse:
    """Remove ``body``'s label from the object (idempotent), and announce a removal that took a label off."""
    canonical = fga.canonical_object_id(list(segments))
    try:
        removed = await run_in_threadpool(
            object_labels.detach, settings.registry_root, settings.storage_options(), kind, uuid, name=body.name, actor=_actor(token)
        )
    except records.RecordChangedError as exc:
        raise ConcurrentModificationError(f"the labels of {canonical} kept changing; remove the label again") from exc
    if removed:
        await emit_control(
            control,
            action=_LABELLED[kind][1],
            object_type=kind,
            object_id=_object(kind, uuid),
            object_name=canonical,
            actor=_actor(token),
            extra={"label": body.name},
        )
    return await _describe(settings, kind=kind, segments=segments, uuid=uuid)


@table_router.post("/{id}/labels/describe", response_model_exclude_none=True)
async def describe_table_labels(id: str, table: ResolvedTableDep, settings: SettingsDep) -> ObjectLabelsResponse:
    """The labels the table carries, each with its value (``None`` for a marker). Read at ``can_get_metadata``."""
    return await _describe(settings, kind="table", segments=table.segments, uuid=table.require_uuid())


@table_router.post("/{id}/labels/set", response_model_exclude_none=True)
async def set_table_label(
    id: str, body: SetLabelRequest, ns: NamespaceDep, table: ResolvedTableDep, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep
) -> ObjectLabelsResponse:
    """Attach a label the table's project defines, or change the value it carries; the table's labels after. Asked at
    ``can_label``, the table owner's."""
    return await _set(ns, settings, control, token, body, kind="table", segments=table.segments, uuid=table.require_uuid(), location=str(table.location))


@table_router.post("/{id}/labels/remove", response_model_exclude_none=True)
async def remove_table_label(
    id: str, body: RemoveLabelRequest, table: ResolvedTableDep, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep
) -> ObjectLabelsResponse:
    """Remove a label from the table (idempotent); the table's labels after. Asked at ``can_label``."""
    return await _remove(settings, control, token, body, kind="table", segments=table.segments, uuid=table.require_uuid())


async def _namespace(id: str, ns: NamespaceDep, settings: Settings) -> fga_deps.GovernedObject:
    segments = parse_identifier(id)
    if len(segments) < 2:
        raise InvalidInputError("labels attach to a namespace inside a warehouse, not to the root or a warehouse")
    return await fga_deps.identify(ns, settings, "namespace", segments)


@namespace_router.post("/{id}/labels/describe", response_model_exclude_none=True)
async def describe_namespace_labels(id: str, ns: NamespaceDep, settings: SettingsDep) -> ObjectLabelsResponse:
    """The labels the namespace carries. Read at ``can_get_metadata``."""
    namespace = await _namespace(id, ns, settings)
    return await _describe(settings, kind="namespace", segments=namespace.segments, uuid=namespace.uuid)


@namespace_router.post("/{id}/labels/set", response_model_exclude_none=True)
async def set_namespace_label(
    id: str, body: SetLabelRequest, ns: NamespaceDep, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep
) -> ObjectLabelsResponse:
    """Attach a label the namespace's project defines; the namespace's labels after. Asked at ``can_label``."""
    namespace = await _namespace(id, ns, settings)
    return await _set(ns, settings, control, token, body, kind="namespace", segments=namespace.segments, uuid=namespace.uuid, location=None)


@namespace_router.post("/{id}/labels/remove", response_model_exclude_none=True)
async def remove_namespace_label(
    id: str, body: RemoveLabelRequest, ns: NamespaceDep, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep
) -> ObjectLabelsResponse:
    """Remove a label from the namespace (idempotent). Asked at ``can_label``."""
    namespace = await _namespace(id, ns, settings)
    return await _remove(settings, control, token, body, kind="namespace", segments=namespace.segments, uuid=namespace.uuid)


def _project(id: str) -> str:
    if not CONTROL_ID_RE.match(id):
        raise InvalidInputError(f"invalid project id {id!r}: must match {CONTROL_ID_RE.pattern}")
    return id


async def _registered(settings: Settings, project: str) -> str:
    """``project``, once its gate passed, if the registry holds it.

    Raises:
        TableNotFoundError: It does not.
    """
    if await run_in_threadpool(project_registry.get_project, settings.registry_root, settings.storage_options(), project) is None:
        raise TableNotFoundError(f"project not found: {project}")
    return project


async def _readable_project(id: str, settings: Settings, token: Caller | None, client: FgaClientDep) -> str:
    """``id``, for a caller its project list shows the project to: a member, or the estate admin. Anyone else, and a
    project the registry does not hold, is answered the missing project's 404 ([[LH-587]])."""
    project = _project(id)
    doors = [("can_list_all_projects", settings.fga_root_object), ("member", f"project:{project}")]
    try:
        await fga_deps.require_any_relation(client, settings, token, doors=doors)
    except PermissionDeniedError as exc:
        raise TableNotFoundError(f"project not found: {project}") from exc
    return await _registered(settings, project)


async def _administered_project(id: str, settings: Settings, token: Caller | None, client: FgaClientDep) -> str:
    """``id``, for its project's admin; a project the registry does not hold is not found, after the gate."""
    project = _project(id)
    await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")
    return await _registered(settings, project)


async def _standing(settings: Settings, found: Sequence[Attachment]) -> list[tuple[Attachment, str]]:
    """Each of ``found`` whose object stands, with the name it holds now.

    An object in the trash keeps its records for an undrop and is addressed by nobody meanwhile, so it is neither listed
    nor counted as carrying a label; a table is named by its location's claim, which a rename moves, and one whose
    claim no longer names it is gone.
    """
    root, so, store = settings.registry_root, settings.storage_options(), table_claims.claim_store(settings)

    def judged(attachment: Attachment) -> str | None:
        if trash.keeps(root, so, attachment.uuid, kind=attachment.kind):
            return None
        if attachment.kind == "table":
            return table_claims.current_name(store, attachment.location, attachment.uuid) if attachment.location else None
        return attachment.id

    names = await asyncio.gather(*(run_in_threadpool(judged, attachment) for attachment in found))
    return [(attachment, name) for attachment, name in zip(found, names, strict=True) if name is not None]


@projects_router.get("/{id}/labels", response_model_exclude_none=True)
async def list_label_definitions(id: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> list[LabelDefinition]:
    """Every label the project defines, by name, for a member of it or the estate admin."""
    project = await _readable_project(id, settings, token, client)
    return await run_in_threadpool(object_labels.definitions, settings.registry_root, settings.storage_options(), project)


@projects_router.post("/{id}/labels/define", response_model_exclude_none=True)
async def define_label(
    id: str, body: LabelDefinitionRequest, settings: SettingsDep, token: CurrentToken, client: FgaClientDep, control: ControlEmitterDep
) -> LabelDefinition:
    """Define a label, or evolve the definition its name holds: its scope and allowed values only grow and its kind
    never changes, so every label already attached stays valid. A definition applied again as it stands announces
    nothing. The project's admin's act."""
    project = await _administered_project(id, settings, token, client)
    asked = object_labels.proposed(
        project=project,
        name=body.name,
        description=body.description,
        scope=body.scope,
        kind=body.kind,
        allowed_values=body.allowed_values,
        actor=_actor(token),
    )
    try:
        defined, changed = await run_in_threadpool(object_labels.define, settings.registry_root, settings.storage_options(), asked)
    except records.RecordChangedError as exc:
        raise ConcurrentModificationError(f"label {asked.name!r} kept changing; define it again") from exc
    if not changed:
        return defined
    await emit_control(
        control,
        action="label_defined",
        object_type="label",
        object_id=object_labels.label_object(defined.uuid),
        actor=_actor(token),
        extra={"project": project, "name": defined.name},
    )
    return defined


@projects_router.post("/{id}/labels/{name}/delete", response_model_exclude_none=True)
async def delete_label_definition(
    id: str, name: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep, control: ControlEmitterDep
) -> LabelDefinition:
    """Delete a label the project defines, once no standing object of the project carries it (409 otherwise, naming
    each as it is named now). An object in the trash does not hold the definition: restored after the delete, it shows
    no such label. The project's admin's act."""
    project = await _administered_project(id, settings, token, client)
    root, so = settings.registry_root, settings.storage_options()
    deleted = await run_in_threadpool(object_labels.definition, root, so, project, object_labels.require_name(name))
    if deleted is None:
        raise TableNotFoundError(f"label not found: {name}")
    carried = sorted(named for _, named in await _standing(settings, await run_in_threadpool(object_labels.carriers, root, so, deleted)))
    if carried:
        raise NamespaceNotEmptyError(f"label {name!r} is attached to {len(carried)} object(s) ({', '.join(carried[:5])}); remove it from each first")
    await run_in_threadpool(object_labels.delete_definition, root, so, deleted)
    await emit_control(
        control,
        action="label_deleted",
        object_type="label",
        object_id=object_labels.label_object(deleted.uuid),
        actor=_actor(token),
        extra={"project": project, "name": deleted.name},
    )
    return deleted


@projects_router.get("/{id}/labels/objects", response_model_exclude_none=True)
async def list_labelled_objects(
    id: str,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    label: Annotated[str | None, Query(description="Only the objects carrying this label.")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    page_token: Annotated[str | None, Query(description="Continue after this object (the previous page's `page_token`).")] = None,
) -> LabelledObjectsResponse:
    """The project's labelled tables and namespaces the caller may see (``can_get_metadata`` on each, judged on its
    uuid), each named as it is named now, with the labels whose definition stands. An object that is gone or in the
    trash is listed to nobody. Paged in the order of the objects' kinds and uuids."""
    project = await _readable_project(id, settings, token, client)
    so = settings.storage_options()
    defined = await _defined(settings, project)
    found = await run_in_threadpool(object_labels.attachments, settings.registry_root, so, project=project)
    candidates: list[tuple[str, ObjectLabelsResponse]] = []
    for attachment, name in await _standing(settings, found):
        shown = object_labels.labels_shown(attachment, defined)
        if not shown or (label is not None and label not in shown):
            continue
        candidates.append((f"{attachment.kind}:{attachment.uuid}", ObjectLabelsResponse(kind=attachment.kind, id=name, uuid=attachment.uuid, labels=shown)))
    candidates.sort(key=lambda candidate: candidate[0])

    async def judged_on(batch: Sequence[ObjectLabelsResponse]) -> list[str | None]:
        return [_object(item.kind, item.uuid) for item in batch]

    page, next_token = await fga_deps.judged_page(
        fga_deps.authorization(settings, client, token),
        relation="can_get_metadata",
        candidates=candidates,
        objects_of=judged_on,
        page_token=page_token,
        limit=limit,
    )
    return LabelledObjectsResponse(project=project, objects=list(page), page_token=next_token)
