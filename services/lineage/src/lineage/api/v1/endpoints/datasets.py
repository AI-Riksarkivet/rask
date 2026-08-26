"""Dataset-level lineage query endpoints (upstream / downstream / producers / creator / schema / graph).

Every route is gated on OpenFGA ``can_get_metadata`` for ``{name}`` (router-level
``require_metadata_access``); related datasets the caller may not see are dropped via the
per-request :class:`~lineage.api.fga_deps.DatasetFilter`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from lineage.api.dependencies import RepositoryDep
from lineage.api.fga_deps import FilterDep, audit_read, require_metadata_access, require_write_access
from lineage.schemas import Creator, DatasetSchema, LineageGraph, Neighbors, Producers, Readers
from lineage.services.repository import MAX_WALK_DEPTH


# require_metadata_access gates the read (must run first); audit_read then logs the now-authorized
# access (#6). Router-level deps run in declaration order, so the gate precedes the log.
router = APIRouter(
    prefix="/datasets",
    tags=["query"],
    dependencies=[Depends(require_metadata_access), Depends(audit_read)],
)


@router.get("/{name}/upstream")
async def get_upstream(name: str, repository: RepositoryDep, datasets: FilterDep) -> Neighbors:
    """What ``name`` was derived from (provenance).

    Gated on ``can_get_metadata`` for ``name``; related datasets the caller may not see are
    dropped so the graph can't disclose tables outside its reach.
    """
    result = await repository.upstream(name)
    visible = await datasets.visible([ref.name for ref in result.related])
    result.related = [ref for ref in result.related if ref.name in visible]
    return result


@router.get("/{name}/downstream")
async def get_downstream(name: str, repository: RepositoryDep, datasets: FilterDep) -> Neighbors:
    """What derives from ``name`` (impact). Gated; non-visible related datasets are dropped."""
    result = await repository.downstream(name)
    visible = await datasets.visible([ref.name for ref in result.related])
    result.related = [ref for ref in result.related if ref.name in visible]
    return result


@router.get("/{name}/producers")
async def get_producers(name: str, repository: RepositoryDep) -> Producers:
    """The runs that wrote ``name`` — who / when / how. Gated on ``can_get_metadata``."""
    return await repository.producers(name)


@router.get("/{name}/readers", dependencies=[Depends(require_write_access)])
async def get_readers(
    name: str,
    repository: RepositoryDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> Readers:
    """Who has READ ``name`` — the read-audit log's query surface (#41 was capture-only until now).

    Owner/writer-gated (``require_write_access``, on TOP of the router's ``can_get_metadata``): an access
    log reveals *who* touched a dataset, so only a data owner may audit it — a casual reader can trace the
    dataset's provenance but not enumerate who else viewed it. Aggregated per principal (last read + count,
    newest first). Empty when read-auditing is/was off (the log table exists but has no rows).
    """
    return await repository.readers(name, limit)


@router.get("/{name}/creator")
async def get_creator(name: str, repository: RepositoryDep) -> Creator:
    """Who created ``name`` (the verified catalog principal). Gated on ``can_get_metadata``."""
    return await repository.creator(name)


@router.get("/{name}/schema")
async def get_schema(name: str, repository: RepositoryDep, version: int | None = None) -> DatasetSchema:
    """The persisted column schema for ``name`` — at ``?version=N`` if given, else the latest. (#24)

    Captured from the standard ``SchemaDatasetFacet`` at ingest and stored per-version on the ``WROTE``
    edge (previously the facet was received and discarded). Gated on ``can_get_metadata`` for ``name``;
    this is the prerequisite for column-level lineage and powers schema-diffing between Lance versions.
    """
    return await repository.dataset_schema(name, version)


@router.get("/{name}/graph")
async def get_graph(
    name: str,
    repository: RepositoryDep,
    datasets: FilterDep,
    depth: Annotated[int | None, Query(ge=1, le=MAX_WALK_DEPTH)] = None,
) -> LineageGraph:
    """The connected lineage subgraph around ``name`` (nodes + edges) for a DAG view.

    ``depth`` bounds the walk to that many hops in each direction, making this a ROOTED
    NEIGHBOURHOOD rather than a whole connected component — the shape Marquez serves at
    ``/lineage?nodeId=&depth=N``. Omitted, the walk is unbounded, which is the previous behaviour and
    still what an un-rooted caller wants.

    Bounding here rather than in the client is the point: a UI depth control that filters an
    already-fetched window cannot reach anything the window excluded, however small the depth.

    `ge=1, le=MAX_WALK_DEPTH` is the FIRST of two guards and the one that answers the caller: a bad
    depth is a 422 here rather than a 500 from the repository. The repository re-checks anyway,
    because the hop range is interpolated into Cypher and that check must not depend on every caller
    having gone through this route.

    Nodes the caller may not see (and edges touching them) are dropped; the requested ``name``
    is already authorized by the route gate.
    """
    result = await repository.graph(name, depth)
    visible = await datasets.visible([node.id for node in result.nodes if node.id != name])
    visible.add(name)
    result.nodes = [node for node in result.nodes if node.id in visible]
    result.edges = [e for e in result.edges if e.source in visible and e.target in visible]
    return result
