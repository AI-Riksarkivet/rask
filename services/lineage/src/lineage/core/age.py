"""Thin Apache AGE client over psycopg.

AGE is queried with openCypher wrapped in SQL: ``SELECT * FROM cypher('<graph>',
$$ ... $$, <params>) AS (col agtype, ...)``. Rules learned the hard way:

* The graph name is a parse-time literal — it cannot be a bind parameter, so we
  inline it (validated as an identifier; it comes from config, never user input).
* Data flows through the ``params`` agtype (a JSON object), referenced as ``$key``
  in the Cypher — never string-interpolated.
* AGE rejects a ``params`` argument when the query has none, so we omit it then.
* Results come back as ``agtype`` text; vertices/edges carry a ``::vertex`` suffix.
"""

from __future__ import annotations

import json
import re
from typing import Any, LiteralString

from psycopg import AsyncConnection, sql
from psycopg_pool import AsyncConnectionPool


_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


async def _configure(conn: AsyncConnection) -> None:
    """Per-connection setup: load AGE and put its catalog on the search path.

    Autocommit so these session statements don't leave the connection in a
    transaction (the pool discards connections left ``INTRANS`` by ``configure``).
    """
    await conn.set_autocommit(True)
    await conn.execute("LOAD 'age'")
    await conn.execute('SET search_path = ag_catalog, "$user", public')


def make_pool(database_url: str, *, statement_timeout_seconds: float = 30.0) -> AsyncConnectionPool:
    """Build a (closed) async connection pool; call ``await pool.open()`` in the lifespan.

    ``check`` pings each connection on checkout so a Postgres restart/failover doesn't hand out dead
    sockets (the pool discards + replaces them instead of surfacing an OperationalError to the request).
    ``statement_timeout`` (server-side, per statement) bounds a runaway Cypher traversal — e.g. an
    unbounded ``*1..`` path over a grown graph — so it can't pin a pooled connection forever; every
    statement here is a point MERGE/MATCH or a modest fetch-all, so a generous timeout only trips on
    genuine runaways.
    """
    return AsyncConnectionPool(
        database_url,
        configure=_configure,
        open=False,
        min_size=1,
        max_size=8,
        check=AsyncConnectionPool.check_connection,
        kwargs={"options": f"-c statement_timeout={int(statement_timeout_seconds * 1000)}"},
    )


def _parse(value: Any) -> Any:
    """Parse one ``agtype`` cell into a Python value (handles vertex/edge/path suffixes)."""
    if not isinstance(value, str):
        return value
    for suffix in ("::vertex", "::edge", "::path"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _sql(graph: str, query: LiteralString, *, with_params: bool, columns: int) -> sql.Composed:
    """Compose the AGE wrapper SQL safely with ``psycopg.sql``.

    ``cypher()`` parses its graph name and query text at plan time, so neither can
    be a bind parameter: the graph is embedded as a SQL literal (only after an
    identifier-shape check) and the Cypher as raw SQL inside the ``$$ … $$`` dollar
    quoting AGE requires — typed ``LiteralString`` so only trusted, code-defined
    Cypher reaches it. Row params still flow through the trailing ``%s`` agtype bind.
    """
    if not _IDENT.match(graph):
        raise ValueError(f"invalid graph name: {graph!r}")
    cols = sql.SQL(", ").join(sql.SQL("{} agtype").format(sql.Identifier(f"c{i}")) for i in range(max(columns, 1)))
    parts: list[sql.Composable] = [
        sql.SQL("SELECT * FROM cypher("),
        sql.Literal(graph),
        sql.SQL(", $$ "),
        sql.SQL(query),
        sql.SQL(" $$"),
    ]
    if with_params:
        parts.append(sql.SQL(", %s"))
    parts += [sql.SQL(") AS ("), cols, sql.SQL(")")]
    return sql.Composed(parts)


async def run_cypher(
    conn: AsyncConnection,
    graph: str,
    query: LiteralString,
    params: dict[str, Any] | None = None,
    *,
    columns: int = 1,
) -> list[list[Any]]:
    """Run one Cypher statement on an existing connection; return parsed rows."""
    statement = _sql(graph, query, with_params=bool(params), columns=columns)
    args = (json.dumps(params),) if params else ()
    cur = await conn.execute(statement, args)
    return [[_parse(cell) for cell in row] for row in await cur.fetchall()]


async def fetch(
    pool: AsyncConnectionPool,
    graph: str,
    query: LiteralString,
    params: dict[str, Any] | None = None,
    *,
    columns: int = 1,
) -> list[list[Any]]:
    """Run one read query on a pooled connection; return parsed rows."""
    async with pool.connection() as conn:
        return await run_cypher(conn, graph, query, params, columns=columns)
