"""Every `Run` property the queries SORT BY must carry a lookup index.

the lakehouse register, row E5 (drained 2026-09-10; in git history) ("no index on `Run.event_time`", O(history) hot paths).

MEASURED ON THE DEPLOYED GRAPH 2026-09-07. `lineage.Run` carried exactly one index —
`lineage_run_uniq`, the MERGE key — while **six** queries in `cypher.py` order by `r.event_time`,
including the runs board itself (`list_runs_page`: `… ORDER BY r.event_time DESC LIMIT n`). Every one
of them sorts the whole label table: 5,586 Run nodes, 4,176 kB, growing, with
`LINEAGE_RUN_RETENTION_DAYS=0` on the deployed service and `runRetentionDays: 0` in the chart — so the
pruning mechanism EXISTS and ships off, and nothing bounds the table it scans.

**BOUNDING A RESPONSE IS NOT BOUNDING THE WORK, and that is worth stating because this estate bounded
the response first.** `/runs` was capped at 200 rows earlier the same day (the board was returning
5,122 runs / 2.65 MB); the LIMIT bounds what crosses the wire, and the ORDER BY still reads and sorts
every row behind it. An index is what bounds the second half.

THE ASSERTION IS DERIVED, NOT LITERAL. It reads the ORDER BY properties out of `cypher.py`'s own
source and demands each has an entry in `postgres.VERTEX_LOOKUP_KEYS` — so a NEW sorted property fails
here rather than silently adding a seq-scan, and renaming one cannot leave a stale literal passing. The
estate has already been bitten by the other shape today: a gate pinning the literal `/runs` went red
when the code correctly stopped using that route.

The index itself needs no new machinery: `repository.ensure_graph_constraints()` already creates every
`VERTEX_LOOKUP_KEYS` entry idempotently on each boot, best-effort, with `CREATE INDEX IF NOT EXISTS`.
"""

from __future__ import annotations

import re
from pathlib import Path

from lineage.services import postgres as pg


#: `ORDER BY r.<prop>` in the run queries — the properties a sort actually reads.
_ORDER_BY_RUN_PROP = re.compile(r"ORDER BY\s+r\.([A-Za-z_][A-Za-z0-9_]*)")


def _sorted_run_properties() -> set[str]:
    source = (Path(pg.__file__).parent / "cypher.py").read_text(encoding="utf-8")
    return set(_ORDER_BY_RUN_PROP.findall(source))


def _indexed_run_properties() -> set[str]:
    indexed: set[str] = set()
    for label, keys in (*pg.VERTEX_UNIQUE_KEYS, *pg.VERTEX_LOOKUP_KEYS):
        if label == "Run":
            indexed.update(keys)
    return indexed


def test_the_queries_really_do_sort_runs() -> None:
    """The gate's own precondition: if nothing sorts, the demand below is vacuous."""
    sorted_props = _sorted_run_properties()
    assert sorted_props, "no `ORDER BY r.<prop>` found in cypher.py — the extraction has drifted from the source"


def test_every_sorted_Run_property_is_indexed() -> None:
    """The headline: a property the queries order by must not be a full-table sort of a growing label."""
    missing = sorted(_sorted_run_properties() - _indexed_run_properties())
    assert not missing, (
        f"these Run properties are ORDERed BY in cypher.py but have no index declared in "
        f"postgres.VERTEX_UNIQUE_KEYS / VERTEX_LOOKUP_KEYS: {missing}. Each one sorts the whole Run label "
        "table on every call, and nothing bounds that table — run retention ships off"
    )


def test_the_lookup_entry_is_shaped_for_the_index_builder() -> None:
    """`ensure_graph_constraints` builds one property-access term per key, so a key must be a plain
    property name — a dotted or expression-shaped key would produce an index on something else."""
    for label, keys in pg.VERTEX_LOOKUP_KEYS:
        for key in keys:
            assert key.isidentifier(), f"{label} lookup key {key!r} is not a plain property name"
