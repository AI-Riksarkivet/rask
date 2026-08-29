"""The column-level graph must reach past one hop.

WHY. `dataset_column_graph` asks for "every column edge touching THIS dataset" and stops. That is
one hop by construction: a field two derivations upstream — the actual answer to "where did this
column come from" whenever a table is built from a table that was built from something — is not in
the payload, and no client setting could ask for it. The table-level graph grew a depth control
(`open_lineage_graph.md` P1 #7); the column graph had none to grow, which is P3's column-depth item.

WHAT DEPTH COUNTS, and it is a deliberate choice rather than the only one available: DATASET hops.
The underlying query is dataset-scoped, the view draws one container per table, and "one more table
out" is the unit a person asking this question thinks in. Counting column hops would expand along
one field at a time and give a container holding a single column, which reads as a broken table
rather than a bounded walk.

The expansion is a bounded LOOP in Python over a parameterised query, not an interpolated
variable-length walk — the frontier is a bind parameter, so unlike the table-level walk there is no
string to sanitise. The bound is still checked, because an unbounded expansion over a connected
estate is the whole graph.
"""

from __future__ import annotations

from typing import Any

import pytest
from lineage.services import repository as repo_mod
from lineage.services.cypher import MAX_COLUMN_DEPTH
from lineage.services.repository import LineageRepository


def _edge(o_ds: str, o_fld: str, i_ds: str, i_fld: str) -> list[Any]:
    """One `_DATASET_COLUMN_EDGES` row: output column derived from input column, no transformation."""
    return [o_ds, o_fld, i_ds, i_fld, "IDENTITY", "", False, ""]


#: gold ← silver ← bronze. Rooted at gold, one dataset hop reaches silver and TWO reach bronze — so
#: bronze's presence is exactly what separates a depth-2 answer from a depth-1 one.
_CHAIN = {
    "gold$report": [_edge("gold$report", "total", "silver$features", "amount")],
    "silver$features": [
        _edge("gold$report", "total", "silver$features", "amount"),
        _edge("silver$features", "amount", "bronze$events", "raw_amount"),
    ],
    "bronze$events": [_edge("silver$features", "amount", "bronze$events", "raw_amount")],
}


def _fake_fetch(_pool: Any, _graph: str, query: str, params: dict[str, Any] | None = None, *, columns: int = 1) -> Any:
    del columns

    async def _run() -> list[list[Any]]:
        if "HAS_COLUMN" in query:
            return [["total", "int64"]]
        # The frontier query. Every edge touching any dataset in `$dss`, deduplicated by the caller.
        frontier = (params or {}).get("dss") or [(params or {}).get("ds")]
        rows: list[list[Any]] = []
        for ds in frontier:
            rows.extend(_CHAIN.get(str(ds), []))
        return rows

    return _run()


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> LineageRepository:
    monkeypatch.setattr(repo_mod, "fetch", _fake_fetch)
    return LineageRepository(pool=None, graph="g")  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_one_hop_is_the_default_and_stops_at_the_neighbour(repo: LineageRepository) -> None:
    """Depth 1 must be exactly what this returned before the parameter existed."""
    graph = await repo.dataset_column_graph("gold$report")
    datasets = {c.dataset for c in graph.columns}
    assert datasets == {"gold$report", "silver$features"}, (
        f"depth 1 reached {sorted(datasets)} — it must stay the single-hop answer, or adding the parameter silently changed every existing caller's result"
    )


@pytest.mark.asyncio
async def test_a_deeper_walk_reaches_the_table_behind_the_table(repo: LineageRepository) -> None:
    """The finding itself: two hops must surface the source a field ACTUALLY came from."""
    graph = await repo.dataset_column_graph("gold$report", depth=2)
    datasets = {c.dataset for c in graph.columns}
    assert "bronze$events" in datasets, "depth 2 did not reach the dataset two derivations upstream, which is the only thing that distinguishes it from depth 1"
    assert any(e.source_dataset == "bronze$events" for e in graph.edges), (
        "the second-hop columns arrived without the edge that explains them — a node with no "
        "derivation renders as an unconnected field, which is worse than not fetching it"
    )


@pytest.mark.asyncio
async def test_the_walk_terminates_on_a_cycle(repo: LineageRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    """A → B → A must not expand forever, and must not duplicate its edges.

    Column lineage is emitted per run, and a job that reads and writes the same table across runs
    produces exactly this shape — the same aggregation-across-runs that inflated the table graph to a
    38,430px canvas. A visited-set is what makes the loop bound the work rather than just the hops.
    """
    cyclic = {
        "a$t": [_edge("a$t", "x", "b$t", "y"), _edge("b$t", "y", "a$t", "x")],
        "b$t": [_edge("a$t", "x", "b$t", "y"), _edge("b$t", "y", "a$t", "x")],
    }

    def _cyclic_fetch(_pool: Any, _graph: str, query: str, params: dict[str, Any] | None = None, *, columns: int = 1) -> Any:
        del columns

        async def _run() -> list[list[Any]]:
            if "HAS_COLUMN" in query:
                return [["x", "int64"]]
            rows: list[list[Any]] = []
            for ds in (params or {}).get("dss") or []:
                rows.extend(cyclic.get(str(ds), []))
            return rows

        return _run()

    monkeypatch.setattr(repo_mod, "fetch", _cyclic_fetch)
    graph = await repo.dataset_column_graph("a$t", depth=MAX_COLUMN_DEPTH)
    assert len(graph.edges) == 2, f"a cycle produced {len(graph.edges)} edges from 2 distinct derivations"


@pytest.mark.parametrize("bad", [0, -1, MAX_COLUMN_DEPTH + 1, "2", 1.5])
@pytest.mark.asyncio
async def test_an_out_of_range_depth_is_refused(repo: LineageRepository, bad: object) -> None:
    """Refused, not clamped: an unbounded expansion over a connected estate is the whole graph, and
    silently answering a smaller question than the caller asked hides that they asked it."""
    with pytest.raises((ValueError, TypeError)):
        await repo.dataset_column_graph("gold$report", depth=bad)  # ty: ignore[invalid-argument-type]
