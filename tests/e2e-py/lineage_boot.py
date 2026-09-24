"""Build a `LineageRepository` the way the lineage service's lifespan builds one.

`ingest_event` writes the AGE graph AND a row in the durable `public.lineage_events` feed, and the
feed table is provisioned at BOOT (`main.py` → `ensure_events_table`), not on first write. A suite
that constructs the repository directly therefore has to boot it too, or its first ingest dies
`UndefinedTable` against a database that is otherwise perfectly healthy.

THE SUITE HID THAT BEHIND FILE ORDER. Ten tests hand-rolled `LineageRepository(pool, "lineage")`;
two of them happened to call `ensure_events_table` because they are ABOUT that call, and every test
after those two inherited a table it never asked for. So the three tests that ran BEFORE them failed
and the five after them passed — one defect, sorted by line number. Order-dependence like that is
invisible while the file is read top to bottom and fails the moment anything reorders or parallelises.

So the boot sequence lives in ONE place and mirrors `main.py`'s: a step added to the service's
lifespan reaches every test by being added here, instead of reaching whichever tests were written
after it. Pinned by `tests/unit/test_an_e2e_test_that_ingests_boots_the_repository_first.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from lineage.services.repository import LineageRepository


async def booted_repository(pool: AsyncConnectionPool, graph: str = "lineage") -> LineageRepository:
    """A repository whose storage exists — the same four steps, in the same order, as the lifespan.

    `ensure_reads_table` is here because `main.py` calls it unconditionally: the read-audit feature is
    gated by a setting, its TABLE is not, and a suite that provisioned a different set of tables than
    production would be a faithful-looking model of something nobody runs.
    """
    from lineage.services.repository import LineageRepository

    repository = LineageRepository(pool, graph)
    await repository.ensure_events_table()
    await repository.ensure_reads_table()
    await repository.ensure_graph()
    await repository.ensure_graph_constraints()
    return repository
