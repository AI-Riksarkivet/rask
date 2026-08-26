"""The lineage walk must be BOUNDABLE, and the bound must reach Cypher safely.

WHY THIS EXISTS. `_UPSTREAM`/`_DOWNSTREAM` walk `DERIVED_FROM*1..` — unbounded — so the only rooted
read the service offered returned an entire connected component. The graph UI therefore could not ask
"the neighbourhood around this node", and its depth control had nothing to bound at the source: it
fetched a globally-capped estate window and FILTERED it in the browser. Measured consequences of that,
both in `open_lineage_graph.md`: a search can only reach nodes already drawn (P1 #8), and depth cannot
change what is fetched (P1 #7). Marquez bounds server-side — `/lineage?nodeId=&depth=N` — which is the
shape this restores.

WHY THE BOUND IS INTERPOLATED, AND WHY THAT NEEDS A TEST. openCypher takes the hop range as SYNTAX
(`*1..3`), not as a bind parameter: `*1..$depth` does not parse. So the number must be formatted into
the query string, which is exactly the shape SQL injection takes. The depth is therefore coerced to a
bounded int before it ever reaches the string, and this pins that — a regression here is not a wrong
graph, it is arbitrary Cypher against the estate's lineage store.

The tests read the built query rather than executing it: these suites run with no AGE behind them, and
the property that matters (what SQL is produced for a given depth) is fully decided before execution.
"""

from __future__ import annotations

import pytest
from lineage.services.repository import bounded_walk


def test_a_depth_becomes_a_bounded_hop_range() -> None:
    """The whole point: depth 2 must ask for at most two hops, not the whole component."""
    assert "*1..2" in bounded_walk("MATCH (d)-[:DERIVED_FROM*1..]->(u) RETURN u", 2)


def test_no_depth_keeps_the_unbounded_walk() -> None:
    """`None` is the estate read, which is a real answer and must not silently become a bound."""
    query = bounded_walk("MATCH (d)-[:DERIVED_FROM*1..]->(u) RETURN u", None)
    assert "*1.." in query
    assert "*1..0" not in query and "*1..1" not in query


@pytest.mark.parametrize("depth", [1, 2, 3, 9])
def test_every_accepted_depth_lands_in_the_range(depth: int) -> None:
    assert f"*1..{depth}" in bounded_walk("MATCH (d)-[:DERIVED_FROM*1..]->(u) RETURN u", depth)


@pytest.mark.parametrize(
    "hostile",
    [
        "2] RETURN u UNION MATCH (x) DETACH DELETE x //",
        "1; DROP TABLE runs",
        "'; --",
        "999999999999",
        -1,
        0,
    ],
)
def test_a_hostile_or_absurd_depth_cannot_reach_the_query(hostile: object) -> None:
    """A depth is a small positive integer or it is not a depth.

    Anything else must be REFUSED rather than coerced to something plausible: silently clamping a
    string full of Cypher to `1` would run a query the caller did not ask for and hide that they
    tried. The absurd-but-numeric cases are here too, because a caller asking for a billion hops is
    asking for the unbounded walk under a different name.
    """
    with pytest.raises((ValueError, TypeError)):
        bounded_walk("MATCH (d)-[:DERIVED_FROM*1..]->(u) RETURN u", hostile)  # ty: ignore[invalid-argument-type]


def test_the_bound_is_applied_to_every_variable_hop_in_the_query() -> None:
    """A query with two walks must not come back half-bounded.

    Not hypothetical: the rooted read runs an upstream and a downstream walk, and bounding only the
    first would return a neighbourhood that is shallow one way and the whole component the other —
    which reads as a graph bug, not as a missing bound.
    """
    two = "MATCH (d)-[:DERIVED_FROM*1..]->(u) MATCH (d)<-[:DERIVED_FROM*1..]-(x) RETURN u, x"
    assert bounded_walk(two, 3).count("*1..3") == 2
