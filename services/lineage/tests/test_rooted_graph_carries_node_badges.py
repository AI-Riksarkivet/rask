"""A rooted subgraph must carry the same node facts the estate graph does.

WHY. The graph UI reads ONE node shape: each card renders the dataset's written versions and whether
any producing run failed. `estate_graph()` populates those (`_ESTATE_WRITES` folded by `_fold_writes`);
`graph()` — the rooted read — did not, because it predates them.

That asymmetry is invisible until the UI switches source. Pointing the canvas at the rooted read to get
a bounded neighbourhood (P2 #12) would have silently dropped every version chip and every failure badge
from every card, and the graph would still have looked entirely plausible — same nodes, same edges,
just quietly less true. Nothing in the type system objects: the fields are optional, so "absent"
and "this table has never been written" render identically.

This pins the two reads against each other rather than checking either alone, because the property that
matters is that they AGREE. A future field added to one is meant to fail here.
"""

from __future__ import annotations

from typing import Any

import pytest
from lineage.services import repository as repo_mod
from lineage.services.repository import LineageRepository


#: Canned AGE responses selected by what each query asks for, so one fake serves every read the two
#: graph builders make. Every call is recorded, because one of the properties under test is not in the
#: returned graph at all: that the ROOTED rollup is name-scoped rather than folding the whole estate.
def _fake_fetch_factory(calls: list[tuple[str, dict[str, Any] | None]]) -> Any:
    async def _fetch(_pool: Any, _graph: str, query: str, params: dict[str, Any] | None = None, *, columns: int = 1) -> list[list[Any]]:
        del columns
        calls.append((query, params))
        if "DERIVED_FROM*1.." in query and "]->(u:Dataset)" in query:  # upstream
            return [["bronze$events", "bronze"]]
        if "DERIVED_FROM*1.." in query and "<-[" in query:  # downstream
            return [["gold$catalog", "gold"]]
        if "d.source_uri" in query:  # node props
            return [
                ["silver$features", "silver", "s3://b/silver", None],
                ["bronze$events", "bronze", "s3://b/bronze", None],
                ["gold$catalog", "gold", "s3://b/gold", None],
            ]
        if "DERIVED_FROM]->(b:Dataset)" in query:  # edges
            return [["silver$features", "bronze$events"], ["gold$catalog", "silver$features"]]
        if "WROTE" in query:  # the writes rollup
            return [
                ["silver$features", "3", "COMPLETE"],
                ["silver$features", "4", "COMPLETE"],
                ["gold$catalog", "1", "FAIL"],
            ]
        return []

    return _fetch


@pytest.fixture
def calls() -> list[tuple[str, dict[str, Any] | None]]:
    return []


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, dict[str, Any] | None]]) -> LineageRepository:
    monkeypatch.setattr(repo_mod, "fetch", _fake_fetch_factory(calls))
    return LineageRepository(pool=None, graph="g")  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_the_rooted_graph_carries_versions_and_failure(repo: LineageRepository) -> None:
    """The whole finding: a rooted card must know what the estate card knows."""
    result = await repo.graph("silver$features", depth=1)
    by_id = {n.id: n for n in result.nodes}

    assert by_id["silver$features"].versions == ["3", "4"], (
        f"the rooted read reported versions {by_id['silver$features'].versions!r} — a card sourced from "
        "it would render no version chips at all, which is indistinguishable from a table never written"
    )
    assert by_id["gold$catalog"].failed is True, (
        "a FAILED producing run did not reach the rooted node, so the canvas would show a healthy card for a dataset whose last write failed"
    )
    assert by_id["bronze$events"].failed is False, "a dataset with no failing run must not be flagged"


@pytest.mark.asyncio
async def test_rooted_and_estate_agree_on_the_same_dataset(repo: LineageRepository) -> None:
    """Non-vacuity, and the real contract: the two reads must not disagree about one node.

    Asserting the rooted read alone would pass if BOTH were empty. Comparing them is what catches a
    field that gets added to one builder and forgotten in the other.
    """
    rooted = {n.id: n for n in (await repo.graph("silver$features", depth=1)).nodes}
    estate = {n.id: n for n in (await repo.estate_graph()).nodes}

    shared = set(rooted) & set(estate)
    assert shared, "the two reads shared no dataset, so this comparison proves nothing"
    for name in shared:
        assert rooted[name].versions == estate[name].versions, f"{name}: versions disagree between the rooted and estate reads"
        assert rooted[name].failed == estate[name].failed, f"{name}: failure flag disagrees between the rooted and estate reads"


@pytest.mark.asyncio
async def test_the_rooted_rollup_is_scoped_to_the_neighbourhood(repo: LineageRepository, calls: list[tuple[str, dict[str, Any] | None]]) -> None:
    """The rooted read must ask about ITS datasets, not fold the estate's writes to answer.

    Reusing `_ESTATE_WRITES` here would produce identical badges on this fixture and pass the two
    tests above — while making a bounded neighbourhood read scale with the whole estate, which is the
    exact cost P2 #12 exists to remove. So this asserts the query, not the output.
    """
    await repo.graph("silver$features", depth=1)
    writes = [(q, p) for q, p in calls if "WROTE" in q]
    assert writes, "the rooted read made no write-rollup query at all"
    for query, params in writes:
        assert "$names" in query, f"the rooted write rollup is estate-wide: {query}"
        assert params and params.get("names"), "the rooted write rollup was not given a name set to scope by"
