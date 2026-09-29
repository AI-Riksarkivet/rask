"""Every edge label a traversal walks must carry indexes on its endpoint columns.

LH-006. The vertex half of this was closed twice — `VERTEX_UNIQUE_KEYS` for the MERGE keys, then
`VERTEX_LOOKUP_KEYS` for `Run.event_time` — and both times the EDGE tables were left with nothing.

MEASURED ON THE DEPLOYED GRAPH 2026-09-15. `pg_indexes` over the `lineage` schema returned seven rows:
five functional indexes on vertex labels, plus the two `_ag_label_*` primary keys. **No edge label
carried an index at all**, while all seven of them are traversed and none is bounded:

    WROTE 7095 · OF_JOB 6394 · HAS_COLUMN 4236 · READ 1424 · CREATED 1324 · DERIVED_FROM_COLUMN 380 · DERIVED_FROM 70

So a walk cost O(every edge of that label) rather than O(the edges at this node). `EXPLAIN ANALYZE` of
`LATEST_WRITE_VERSION` against the estate's hottest dataset (`acme-silver$features`, 457 WROTE edges)
read all 7,095 WROTE rows and all 7,109 Run rows to answer it — and that query runs INSIDE the ingest
transaction on every event, via `_schema_is_current`.

VERIFIED ON A PROD-SHAPED GRAPH (1,445 datasets / 7,109 runs / 7,095 edges, same 457-edge skew), built
in a throwaway `apache/age:release_PG16_1.5.0` via Dagger so the live graph was never written to:

    no edge index   6.778 ms   Seq Scan on "WROTE" (7,095 rows) + Seq Scan on "Run" (7,109 rows)
    start_id/end_id 1.783 ms   Bitmap Index Scan on lineage_wrote_end (457 rows)

The 3.8x is not the point — the change of ORDER is. The first plan grows with the estate; the second
grows with the node's own degree. `age.py` names exactly this (an unbounded walk over a grown graph) as
why a pooled connection cannot be pinned, and an unbounded correlated walk OOM-killed the AGE container
on 2026-09-15.

THE ASSERTION IS DERIVED, NOT LITERAL — it reads the traversed labels out of `cypher.py`'s own source,
so a NEW edge label fails here rather than silently shipping a full-table walk, and renaming one cannot
leave a stale literal passing. This mirrors `test_a_sorted_run_property_has_an_index.py`, which is the
same gate for the vertex half.

INDEXED ON THE ENDPOINT COLUMNS, NOT ON A PROPERTY: `start_id`/`end_id` are real Postgres columns on
AGE's edge tables, so these are plain btree indexes rather than the `agtype_access_operator` functional
form the vertex keys need. Both directions are indexed because the traversals go both ways — `/upstream`
walks end→start and `/downstream` start→end.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_the_builder_really_emits_the_edge_ddl() -> None:
    """Drive `ensure_graph_constraints` and read the DDL it actually emits.

    The declaration gates above assert on `EDGE_LOOKUP_KEYS` alone, which a builder that never reads it
    would pass — the shape where a test calls the rule instead of the seam. This one calls the seam: a
    recording connection stands in for the pool, and the statements are rendered exactly as psycopg
    would send them.
    """
    from contextlib import asynccontextmanager
    from typing import Any, cast

    from lineage.services.repository import LineageRepository

    emitted: list[str] = []

    class _Conn:
        async def execute(self, statement: Any) -> None:
            emitted.append(statement.as_string(None) if hasattr(statement, "as_string") else str(statement))

    class _Pool:
        @asynccontextmanager
        async def connection(self) -> Any:
            yield _Conn()

    repo = LineageRepository(cast("Any", _Pool()), "lineage")
    await repo.ensure_graph_constraints()

    for label in ("WROTE", "READ", "OF_JOB", "HAS_COLUMN", "CREATED", "DERIVED_FROM", "DERIVED_FROM_COLUMN"):
        for column in ("start_id", "end_id"):
            wanted = f'CREATE INDEX IF NOT EXISTS "lineage_{label.lower()}_{column}" ON "lineage"."{label}" ("{column}")'
            assert wanted in emitted, f"the builder never emitted the {label}.{column} index; got {[s for s in emitted if label in s]}"
