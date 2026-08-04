"""Column-level lineage query endpoints (#24) — field-to-field provenance / impact / subgraph.

Our deepest moat: field-to-field lineage a format-unaware catalog cannot derive. Every route is gated
on OpenFGA ``can_get_metadata`` for the owning ``{name}`` (router-level ``require_metadata_access``); a
column has no ACL of its own (it inherits its table's), so related columns whose owning dataset the
caller can't see are dropped via :func:`~lineage.api.fga_deps.governed` / the per-request filter.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lineage.api.dependencies import RepositoryDep, SettingsDep
from lineage.api.fga_deps import FilterDep, audit_read, governed, require_metadata_access
from lineage.schemas import ColumnGraph, ColumnNeighbors


# Gate first (require_metadata_access), then log the authorized column-level read (#6); router-level
# deps run in declaration order. ``audit_read`` records the owning dataset ``name`` (a column has no
# ACL of its own — it inherits its table's), so the access log is keyed by the same dataset the gate checks.
router = APIRouter(
    prefix="/datasets",
    tags=["query"],
    dependencies=[Depends(require_metadata_access), Depends(audit_read)],
)


@router.get("/{name}/columns/{field}/upstream")
async def get_column_upstream(name: str, field: str, repository: RepositoryDep, datasets: FilterDep, settings: SettingsDep) -> ColumnNeighbors:
    """Column-level provenance (#24): the columns ``name.field`` was (transitively) derived from.

    Our deepest moat — field-to-field lineage a format-unaware catalog cannot derive. Gated on
    ``can_get_metadata`` for the owning ``name``; related columns whose *owning dataset* the caller can't
    see are dropped (a column has no ACL of its own — it inherits its table's), closing the same
    transitive-disclosure hole at column resolution. Auth off → pass-through.
    """
    result = await repository.column_upstream(name, field)
    result.related = await governed(datasets, settings.fga_enabled, result.related, lambda r: {r.dataset})
    return result


@router.get("/{name}/columns/{field}/downstream")
async def get_column_downstream(name: str, field: str, repository: RepositoryDep, datasets: FilterDep, settings: SettingsDep) -> ColumnNeighbors:
    """Column-level impact (#24): the columns (transitively) derived from ``name.field``. Gated +
    governed exactly like the column upstream view — related columns in datasets the caller can't see
    are dropped."""
    result = await repository.column_downstream(name, field)
    result.related = await governed(datasets, settings.fga_enabled, result.related, lambda r: {r.dataset})
    return result


@router.get("/{name}/columns")
async def get_dataset_columns(name: str, repository: RepositoryDep, datasets: FilterDep) -> ColumnGraph:
    """The column-level lineage subgraph around ``name`` (#24) — the field-to-field analogue of
    ``/graph``, for a DAG view of how each column was produced.

    Nodes/edges touching a dataset the caller can't see are dropped (an edge needs BOTH endpoints'
    datasets visible); ``name``'s own columns are always shown (the route gate authorized it).
    """
    result = await repository.dataset_column_graph(name)
    visible = await datasets.visible([n.dataset for n in result.columns if n.dataset != name])
    visible.add(name)
    result.columns = [n for n in result.columns if n.dataset in visible]
    result.edges = [e for e in result.edges if e.source_dataset in visible and e.target_dataset in visible]
    return result
