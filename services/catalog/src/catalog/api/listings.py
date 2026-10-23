"""What a table listing answers when its caller asks only for tables that hold a version ([[LH-528]]).

``include_declared=false`` lists "only tables with storage components" (`lance_docs/ns_catalog/spec.yaml:2356-2367`).
The listing doors ask the backend for every table and judge each one's storage themselves, because the `dir` backend's
own filter misjudges a table registered at a nested location (``dataplane.holds_a_version``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from fastapi.concurrency import run_in_threadpool

from catalog.api.dependencies import Resolver
from catalog.services import dataplane
from catalog.services.table_claims import ResolvedTable
from service_kit.lakehouse.objectfs import StorageOptions


async def holding_a_version(resolver: Resolver, so: StorageOptions, candidates: Sequence[tuple[str, Sequence[str]]]) -> list[tuple[str, list[str]]]:
    """The ``candidates`` whose table holds a committed version, in their order.

    Each candidate is the name the listing answers and the segments of the table it names, judged at the location the
    request's one resolution of that name read, which the authorization filter reads again. The judgments run
    concurrently, bounded by anyio's threadpool, as the backend's own filter probes with bounded concurrency.
    """
    resolved = await asyncio.gather(*(resolver.table(list(segments)) for _, segments in candidates))
    held = await asyncio.gather(*(_holds(table, so) for table in resolved))
    return [(name, list(segments)) for (name, segments), keep in zip(candidates, held, strict=True) if keep]


async def _holds(table: ResolvedTable, so: StorageOptions) -> bool:
    return table.location is not None and await run_in_threadpool(dataplane.holds_a_version, table.location, so)
