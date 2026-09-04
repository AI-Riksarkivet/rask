"""Index endpoints.

A build/drop bumps the Lance version (new manifest) without changing data or columns, so each emits a
best-effort versioned lineage ``WROTE`` event (operation ``create_index`` / ``drop_index``) — provenance of
when a scalar/vector index was (re)built or removed. The native responses carry only a ``transaction_id``,
so the shared ``lineage_deps.emit_measured_write`` trailer reads the produced version back off the dataset
(one open, best-effort — a readback failure never fails the already-committed index op).

**WHERE A BUILD RUNS depends on whether this deployment has an index queue, and the spec asks for the
queued form.** ``CreateTableIndex`` states outright that "index creation is handled asynchronously"
and that progress is monitored through ``ListTableIndices`` / ``DescribeTableIndexStats``; its
response carries an optional ``transaction_id`` and nothing else. So with a queue this door publishes
one ``IndexWorkItem``, answers with that unit's id, and returns in milliseconds — the cost of an index
build is a property of the TABLE, not of the request, and no pod sizing bounds it. Without a queue
nothing would ever execute the unit, so the build runs here as it always has.

The lineage emit belongs to whichever one actually built: a queued unit has produced no version to
measure, and emitting one would put a phantom index event on the graph at every request.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    CreateTableIndexRequest,
    CreateTableIndexResponse,
    CreateTableScalarIndexResponse,
    DescribeTableIndexStatsRequest,
    DescribeTableIndexStatsResponse,
    DescribeTableRequest,
    DescribeTableResponse,
    DropTableIndexRequest,
    DropTableIndexResponse,
    InvalidInputError,
    LanceNamespace,
    ListTableIndicesRequest,
    ListTableIndicesResponse,
)

from catalog.api import lineage_deps
from catalog.api.dependencies import LineageEmitterDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.core.lineage_emit import CREATE_INDEX, DROP_INDEX
from catalog.services import dataplane, native
from service_kit import dapr_publish
from service_kit.lakehouse.work_items import SCALAR_INDEX, VECTOR_INDEX, IndexWorkItem


#: Ceiling for the spec list ops' `limit`. The Lance Namespace spec pages these with
#: `page_token`, so a server answering fewer rows than asked and handing back a token is
#: SPEC-CORRECT — the cap costs a caller nothing but a second call. Declared here rather than
#: clamped in the body so the schema states the real bound. An over-limit request is refused by
#: `install_problem_handlers`, which carries the spec `code` (INVALID_INPUT) a generated client
#: dispatches on.
_MAX_LIST_LIMIT = 1000

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/table", tags=["index"])


@router.post("/{id}/create_index", response_model_exclude_none=True)
async def create_index(
    id: str,
    body: CreateTableIndexRequest,
    request: Request,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> CreateTableIndexResponse:
    """Build a vector index on a table's column — wraps the native ``create_table_index`` op; emits a
    CREATE_INDEX lineage event at the new version."""
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    dataplane.refuse_a_branch_this_door_cannot_honour(body.branch, door="create_table_index")
    if (queued := await _queue_build(request, ns, settings, segments, body, kind=VECTOR_INDEX)) is not None:
        return CreateTableIndexResponse(transaction_id=queued)
    response: CreateTableIndexResponse = await run_in_threadpool(native.call, ns, "create_table_index", body)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=CREATE_INDEX,
        authorization=authorization,
    )
    return response


@router.post("/{id}/create_scalar_index", response_model_exclude_none=True)
async def create_scalar_index(
    id: str,
    body: CreateTableIndexRequest,
    request: Request,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> CreateTableScalarIndexResponse:
    """Build a scalar index on a table's column — wraps the native ``create_table_scalar_index`` op; emits a
    CREATE_INDEX lineage event at the new version."""
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    dataplane.refuse_a_branch_this_door_cannot_honour(body.branch, door="create_table_scalar_index")
    if (queued := await _queue_build(request, ns, settings, segments, body, kind=SCALAR_INDEX)) is not None:
        return CreateTableScalarIndexResponse(transaction_id=queued)
    response: CreateTableScalarIndexResponse = await run_in_threadpool(native.call, ns, "create_table_scalar_index", body)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=CREATE_INDEX,
        authorization=authorization,
    )
    return response


@router.post("/{id}/index/list", response_model_exclude_none=True)
def list_table_indices(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    body: ListTableIndicesRequest | None = None,
    page_token: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=_MAX_LIST_LIMIT)] = None,
    branch: str | None = None,
) -> ListTableIndicesResponse:
    """List the indices defined on a table (paged) — wraps the native ``list_table_indices`` op.

    ``branch`` is DECLARED here only so it can be refused. The route did not accept it at all, which
    read as safe and was not: a caller who asked for a branch's indices got 200 and MAIN's list.
    Verified live 2026-08-31 against a table whose branch carried a BTREE on `id` that main did not —
    this door reported an empty list for the branch, for main, and for a branch never created. Being
    told "no indices" about the wrong dataset is a worse answer than an error, because it is actionable.
    """
    sent = body.model_fields_set if body else frozenset()
    segments = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id if body else None)
    if body is not None:
        page_token = body.page_token if "page_token" in sent else page_token
        limit = body.limit if "limit" in sent else limit
        branch = body.branch if "branch" in sent else branch
    req = ListTableIndicesRequest(id=segments, page_token=page_token, limit=limit, branch=branch, version=body.version if body else None)
    dataplane.refuse_a_branch_this_door_cannot_honour(branch, door="list_table_indices")
    return native.call(ns, "list_table_indices", req)


@router.post("/{id}/index/{index_name}/stats", response_model_exclude_none=True)
def describe_table_index_stats(
    id: str, index_name: str, ns: NamespaceDep, settings: SettingsDep, body: DescribeTableIndexStatsRequest | None = None, branch: str | None = None
) -> DescribeTableIndexStatsResponse:
    """Report stats for a named index on a table — wraps the native ``describe_table_index_stats`` op.

    ``branch`` is declared to be refused, for the same reason as the listing beside it: an index of the
    same name can exist on both refs with different coverage, so answering from main is a plausible
    wrong number rather than a visible failure."""
    segments = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id if body else None)
    if body is not None and "branch" in body.model_fields_set:
        branch = body.branch
    req = DescribeTableIndexStatsRequest(id=segments, index_name=index_name, branch=branch, version=body.version if body else None)
    dataplane.refuse_a_branch_this_door_cannot_honour(branch, door="describe_table_index_stats")
    return native.call(ns, "describe_table_index_stats", req)


@router.post("/{id}/index/{index_name}/drop", response_model_exclude_none=True)
async def drop_table_index(
    id: str,
    index_name: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> DropTableIndexResponse:
    """Drop a named index from a table — wraps the native ``drop_table_index`` op; emits a DROP_INDEX
    lineage event at the new version."""
    segments = parse_identifier(id, settings.delimiter)
    req = DropTableIndexRequest(id=segments, index_name=index_name)
    response: DropTableIndexResponse = await run_in_threadpool(native.call, ns, "drop_table_index", req)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=DROP_INDEX,
        authorization=authorization,
    )
    return response


async def _queue_build(
    request: Request,
    ns: LanceNamespace,
    settings: Settings,
    segments: list[str],
    body: CreateTableIndexRequest,
    *,
    kind: str,
) -> str | None:
    """Publish one build onto the index lane, or ``None`` when this deployment has no queue.

    ``None`` rather than a flag so the caller reads as one decision: either a unit id came back and
    the door answers with it, or the build runs here. Both halves are gated on the SAME topic name
    the maintenance service reads, so a door cannot accept work nothing will ever perform.

    What stays in the handler is the BOUNDED half — parsing the identifier and one ``describe_table``
    to learn where the dataset lives. What leaves is the half whose cost is a property of the data.
    """
    publisher = getattr(request.app.state, "dapr_client", None)
    if not settings.maintenance_index_topic or publisher is None:
        return None
    location = await run_in_threadpool(_table_location, ns, segments)
    item = IndexWorkItem(
        uri=location,
        # The request path IS the identity, so the worker never derives one — and this is the producer
        # that most needs to supply it: a catalog-created table's location may be a path no parser
        # reads back, and without an id the worker falls back to its ambient credential.
        table_id=settings.delimiter.join(segments),
        column=body.column,
        kind=kind,
        index_type=body.index_type or "",
        name=body.name or "",
        params=_pylance_kwargs(body),
    )
    await dapr_publish.publish_event(
        publisher,
        timeout_seconds=settings.control_emit_timeout_seconds,
        pubsub_name=settings.maintenance_index_pubsub,
        topic_name=settings.maintenance_index_topic,
        data=item.model_dump_json(),
        data_content_type="application/json",
    )
    log.info("index_build_queued", extra={"table": item.table_id, "column": item.column, "kind": kind, "index_type": item.index_type})
    return item.unit_id


#: The spec's request field -> pylance's keyword. Only where the two DIFFER; everything else in
#: `_TUNING_FIELDS` passes through under its own name.
#:
#: Translated HERE because this is the party that speaks both vocabularies. A worker doing it would
#: need a namespace handle pointed at the catalog's own root, making a dataset-level service a second
#: writer to `__manifest`.
_SPEC_TO_PYLANCE = {"distance_type": "metric"}

#: What a caller may tune, as the spec's own request declares it. Everything NOT listed is platform
#: routing (`id`, `identity`, `context`, `branch`) or is already a named argument (`column`,
#: `index_type`, `name`) — forwarding either would either duplicate an argument or hand pylance a
#: keyword it has never heard of, which is a TypeError inside the worker rather than a 4xx here.
_TUNING_FIELDS = (
    "distance_type",
    "ascii_folding",
    "base_tokenizer",
    "language",
    "lower_case",
    "max_token_length",
    "remove_stop_words",
    "stem",
    "with_position",
)


def _pylance_kwargs(body: CreateTableIndexRequest) -> dict[str, object]:
    """The tuning this request carries, under the names pylance answers to.

    UNSET FIELDS ARE OMITTED, never sent as ``None``: pylance's defaults are meaningful (a metric of
    ``L2``, a tokenizer chosen per index type) and passing an explicit ``None`` overrides them with
    something no caller asked for.
    """
    tuning: dict[str, object] = {}
    for field in _TUNING_FIELDS:
        value = getattr(body, field, None)
        if value is not None:
            tuning[_SPEC_TO_PYLANCE.get(field, field)] = value
    return tuning


def _table_location(ns: LanceNamespace, segments: list[str]) -> str:
    """Where this table's dataset lives, as the catalog itself answers.

    Asked rather than composed, the I2 rule: a location built from settings and a location the catalog
    vends disagree for most of this estate, and a unit carrying the wrong one indexes another table.
    """
    described: DescribeTableResponse = native.call(ns, "describe_table", DescribeTableRequest(id=segments))
    if not described.location:
        raise InvalidInputError("table has no object-store location to index")
    return described.location
