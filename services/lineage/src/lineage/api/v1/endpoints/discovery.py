"""Discovery endpoints — browse the lineage estate (datasets / jobs / namespaces).

The per-``{name}`` reads in ``datasets.py`` answer *"tell me about X"* — each gated on
``can_get_metadata`` for a name the caller must already know. These answer *"what is there?"*: the
browsable catalog Marquez has and a bare graph store lacks, so a caller (or the UI) never has to know a
name in advance. Each is **governed** exactly like ``/runs`` and ``/events`` — a row referencing a dataset
the caller may not see is dropped, so listing can't disclose tables outside the caller's reach. Auth off →
pass-through.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from lineage.api.dependencies import RepositoryDep, SettingsDep
from lineage.api.fga_deps import FilterDep, governed
from lineage.schemas import (
    Datasets,
    DatasetSummary,
    EstateGraph,
    Jobs,
    Namespaces,
    SearchHit,
    SearchResults,
)


router = APIRouter(tags=["discovery"])

_MAX_LIMIT = 500
_SEARCH_LIMIT = 100


@router.get("/datasets")
async def list_datasets(
    repository: RepositoryDep,
    datasets: FilterDep,
    settings: SettingsDep,
    namespace: str | None = None,
    tag: str | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = 100,
) -> Datasets:
    """Browse every dataset the caller may see — the lineage catalog's landing list.

    Governed (a dataset is shown only if the caller ``can_get_metadata`` on it), optionally filtered by
    ``?namespace=`` / ``?tag=``, and paginated over the *visible* set. This is the entry point the graph
    reads (``/datasets/{name}/...``) needed but could not provide — you no longer must know a name first.
    """
    all_datasets = await repository.list_datasets(namespace=namespace, tag=tag)
    visible = await governed(datasets, settings.fga_enabled, all_datasets, lambda d: {d.name})
    return Datasets(datasets=visible[offset : offset + limit], total=len(visible))


@router.get("/search")
async def search(
    repository: RepositoryDep,
    datasets: FilterDep,
    settings: SettingsDep,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=_SEARCH_LIMIT)] = 25,
) -> SearchResults:
    """Governed search over the discovery estate (P1 Search tier 1, 2026-07-11).

    Case-insensitive substring over dataset NAMES, NAMESPACES, governance TAGS, and the CURRENT
    COLUMN inventory — each hit says WHY it matched (``name`` / ``namespace`` / ``tag:…`` /
    ``column:…``), so "which tables have an embedding column" and "what's in layer=gold" are one
    query. Governance is identical to ``/datasets``: the visibility filter runs over the FULL hit
    set BEFORE the limit, so search can never disclose (or even count) tables outside the caller's
    reach. 📌 Tier 2 (Lance FTS + FLAT vector content search, the rask pattern) stays decision-
    pinned in docs/DECISIONS.md §9 behind its measured recall gate — this tier is metadata-only
    by design.
    """
    needle = q.lower()
    hits: dict[str, SearchHit] = {}

    def hit(summary: DatasetSummary, reason: str) -> None:
        entry = hits.setdefault(
            summary.name,
            SearchHit(name=summary.name, namespace=summary.namespace, tags=summary.tags),
        )
        if reason not in entry.matches:
            entry.matches.append(reason)

    all_datasets = await repository.list_datasets()
    by_name = {d.name: d for d in all_datasets}
    for d in all_datasets:
        if needle in d.name.lower():
            hit(d, "name")
        if d.namespace and needle in d.namespace.lower():
            hit(d, "namespace")
        for tag in d.tags:
            if needle in tag.lower():
                hit(d, f"tag:{tag}")
    for dataset, field in await repository.list_all_columns():
        if needle in field.lower() and dataset in by_name:
            hit(by_name[dataset], f"column:{field}")

    ordered = sorted(hits.values(), key=lambda h: h.name)
    visible = await governed(datasets, settings.fga_enabled, ordered, lambda h: {h.name})
    return SearchResults(query=q, results=visible[:limit], total=len(visible))


@router.get("/graph")
async def estate_graph(
    repository: RepositoryDep,
    datasets: FilterDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = 300,
) -> EstateGraph:
    """The whole visible lineage graph in ONE response — the graph page's bulk read.

    Before this the UI recomposed the estate graph client-side from the per-``{name}`` reads —
    one ``/datasets/{name}/graph`` (plus ``/producers``) per dataset per refresh, a 2N+-request
    storm at N datasets. Governance is identical to ``/datasets``: a node the caller may not see
    is dropped, and an edge needs BOTH endpoints visible (the same transitive-disclosure
    guarantee as the per-dataset graph). Nodes are capped at ``limit`` deterministically (sorted
    by name) with ``total``/``capped`` reporting the truth.
    """
    full = await repository.estate_graph()
    visible = await governed(datasets, settings.fga_enabled, full.nodes, lambda n: {n.id})
    total = len(visible)
    kept = sorted(visible, key=lambda n: n.id)[:limit]
    names = {n.id for n in kept}
    return EstateGraph(
        nodes=kept,
        edges=[e for e in full.edges if e.source in names and e.target in names],
        total=total,
        capped=total > limit,
    )


@router.get("/jobs")
async def list_jobs(repository: RepositoryDep, datasets: FilterDep, settings: SettingsDep) -> Jobs:
    """The jobs (compute identities) that have run — governed by the datasets each wrote.

    A job is shown only if the caller can see every dataset it wrote (a read-only / output-less job is
    hidden when auth is on, mirroring how ``/events`` drops a dataset-less row). Auth off → pass-through.
    """
    all_jobs = await repository.list_jobs()
    visible = await governed(datasets, settings.fga_enabled, all_jobs, lambda j: set(j.outputs))
    return Jobs(jobs=visible, total=len(visible))


@router.get("/namespaces")
async def list_namespaces(repository: RepositoryDep, datasets: FilterDep, settings: SettingsDep) -> Namespaces:
    """The namespaces containing at least one dataset the caller may see (for a namespace-tree browse).

    Derived from the governed dataset set, so a namespace the caller can see no dataset in never appears.
    """
    all_datasets = await repository.list_datasets()
    visible = await governed(datasets, settings.fga_enabled, all_datasets, lambda d: {d.name})
    return Namespaces(namespaces=sorted({d.namespace for d in visible if d.namespace}))
