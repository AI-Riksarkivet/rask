"""Every append-only log this service queries by a column must be indexed on that column.

[[LH-075]]. `public.lineage_reads` is the read-audit the product actually QUERIES: `record_read`
appends to it (`repository.py:1414-1417`) and `readers()` reads it back with
``... WHERE dataset = %s GROUP BY reader`` (`postgres.py:READERS`). Its DDL declared
``seq bigserial PRIMARY KEY, reader text, dataset text, read_at timestamptz`` and nothing else, so
every "who read this dataset" answer sequentially scanned an append-only log that grows without bound.

THE ESTATE ALREADY KNEW THE SHAPE, one table over. `CREATE_EVENTS_RECEIVED_AT_INDEX` indexes
`lineage_events (received_at)` with the rationale stated in its own comment -- "Without it a
time-bounded DELETE seq-scans the whole feed on every retention pass." `lineage_reads` never got the
same treatment, and nothing noticed because a missing index is not an error: it is a query that still
returns the right answer, only slower every day.

DERIVED FROM THE QUERIES, NOT A LIST OF TABLES. The filter column is read out of the SQL this module
declares, so a new query on a new column is covered without anyone remembering to extend this file --
which is the failure mode a hand-written pairing would reproduce.
"""

from __future__ import annotations

import re

from lineage.services import postgres as pg


#: `WHERE <col> = %s` in a SELECT is the access path an index has to serve.
_FILTERED = re.compile(r"FROM\s+(?P<table>public\.\w+)\s+WHERE\s+(?P<col>\w+)\s*=", re.IGNORECASE)

#: `CREATE INDEX ... ON <table> (<col>)`, however the statement is spelled.
_INDEXED = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX[^(]*?ON\s+(?P<table>public\.\w+)\s*\((?P<cols>[^)]+)\)", re.IGNORECASE)

#: `PRIMARY KEY` inside a CREATE TABLE already provides an index on that column.
_PK = re.compile(r"CREATE TABLE[^(]*?(?P<table>public\.\w+)\s*\((?P<body>.+)", re.IGNORECASE | re.DOTALL)


def _statements() -> list[str]:
    """Every SQL literal this module declares."""
    return [v for v in vars(pg).values() if isinstance(v, str) and ("SELECT" in v.upper() or "CREATE" in v.upper())]


def _indexed_columns() -> dict[str, set[str]]:
    """table -> columns reachable by an index, counting a single-column PRIMARY KEY."""
    out: dict[str, set[str]] = {}
    for sql in _statements():
        for m in _INDEXED.finditer(sql):
            cols = {c.strip().split()[0].strip('"') for c in m.group("cols").split(",")}
            out.setdefault(m.group("table").lower(), set()).update(cols)
        for m in _PK.finditer(sql):
            for field in m.group("body").split(","):
                if "PRIMARY KEY" in field.upper():
                    out.setdefault(m.group("table").lower(), set()).add(field.strip().split()[0].strip('"'))
    return out


def test_the_walk_sees_the_schema() -> None:
    """Without this the assertion below passes by matching nothing."""
    assert _statements(), "no SQL literals found in `lineage.services.postgres` -- the module shape changed"
    filtered = [m for sql in _statements() for m in _FILTERED.finditer(sql)]
    assert filtered, "no `... WHERE <col> = %s` query parsed; this gate would check nothing"


def test_every_queried_column_is_reachable_by_an_index() -> None:
    """A filter on an unindexed column of an append-only log degrades with every row written.

    IF THIS IS RED: add `CREATE INDEX IF NOT EXISTS <table>_<col> ON <table> (<col>)` beside the table's
    DDL and execute it where the table is ensured -- the shape `CREATE_EVENTS_RECEIVED_AT_INDEX`
    already uses. Do not silence it by rewriting the query: the access path is the point.
    """
    indexed = _indexed_columns()
    missing = set()
    for sql in _statements():
        for m in _FILTERED.finditer(sql):
            table, col = m.group("table").lower(), m.group("col").lower()
            if col not in {c.lower() for c in indexed.get(table, set())}:
                missing.add(f"{table}({col})")

    assert not missing, (
        f"these columns are FILTERED ON but reachable by no index: {sorted(missing)}. On an append-only "
        "log that is a sequential scan that gets slower with every row, and it never surfaces as an "
        "error -- the query keeps returning the right answer."
    )
