"""The reconcile sweep's SHAPE: one round-trip depth per dataset, and a cron body small enough to read.

Two properties `tests/unit/test_reconcile.py` cannot see, because it drives the sweep end to end and only
looks at what comes out:

* :func:`lineage.core.reconcile.reconcile_all` issues its three independent per-dataset graph lookups
  TOGETHER. Awaited one after another they made every dataset three round-trips deep, so a sweep over an
  estate of N datasets paid 3N serial round-trips to the same graph.
* the cron tick's partitioning of a sweep result is a NAMED, pure function. It used to be inlined in
  ``_on_cron`` — five comprehensions, five WARN branches and two literal 9-key mappings in one 115-line
  body — so the only way to exercise a partition was to run a whole sweep.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from typing import Any, cast

from lineage.api import reconcile_cron
from lineage.core.reconcile import reconcile_all
from lineage.schemas import DatasetSummary, ReconcileState, ReconcileStatus


class _ConcurrencyRepo:
    """A ``_ReconcileRepo`` that records the greatest number of its own reads in flight at once.

    Every read yields to the loop once between claiming and releasing its slot, so the peak is exactly
    the number of reads the caller had outstanding: 1 when they are awaited in sequence, 3 when they are
    issued together.
    """

    def __init__(self) -> None:
        self.inflight = 0
        self.max_inflight = 0

    async def _tracked[T](self, value: T) -> T:
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        await asyncio.sleep(0)
        self.inflight -= 1
        return value

    async def list_datasets(self, namespace: str | None = None, tag: str | None = None) -> list[DatasetSummary]:
        return [DatasetSummary(name="d")]

    async def source_uri(self, name: str) -> str | None:
        return await self._tracked("s3://b/d")

    async def dropped_at(self, name: str) -> str | None:
        return await self._tracked(None)

    async def latest_write_version(self, name: str) -> int | None:
        return await self._tracked(1)

    async def backfill_write(self, name: str, version: int, schema: object | None = None) -> None:
        raise AssertionError("read-only sweep must not back-fill")


async def _no_storage(uri: str) -> int | None:
    return None


def test_reconcile_all_issues_its_three_graph_reads_concurrently() -> None:
    """`source_uri`, `dropped_at` and `latest_write_version` are independent point lookups on the same
    graph; nothing in the sweep needs one to compute another. Awaited in sequence they tripled the
    sweep's round-trip depth for every dataset in the estate."""
    repo = _ConcurrencyRepo()

    asyncio.run(reconcile_all(cast(Any, repo), _no_storage, backfill=False))

    assert repo.max_inflight == 3, f"the three per-dataset graph reads are still serialised (peak in flight: {repo.max_inflight})"


def test_the_cron_partitions_a_sweep_through_a_named_pure_function() -> None:
    """The tick's report is derived from a list of statuses and nothing else, so it belongs in a function
    a test can call with a handful of statuses — not inlined in the request handler, where the only way
    to reach a partition is to drive a whole sweep against a repository double."""
    statuses = [
        ReconcileStatus(dataset="ahead", in_sync=False, status=ReconcileState.STORAGE_AHEAD),
        ReconcileStatus(dataset="lost", in_sync=False, status=ReconcileState.MISSING_ON_STORAGE),
        ReconcileStatus(dataset="blind", in_sync=False, status=ReconcileState.UNREADABLE, unreadable_reason="no creds"),
        ReconcileStatus(dataset="rotten", in_sync=True, status=ReconcileState.IN_SYNC, dangling_blob_columns=["payload"]),
        ReconcileStatus(dataset="old", in_sync=True, status=ReconcileState.IN_SYNC, stale=True),
        ReconcileStatus(dataset="thin", in_sync=True, status=ReconcileState.IN_SYNC, missing_declared_columns=["id"]),
    ]

    report = reconcile_cron.summarize_sweep(statuses)

    assert report.checked == 6
    assert report.backfilled == ["ahead"]
    assert report.storage_loss == ["lost"]
    assert report.unreadable == {"blind": "no creds"}
    assert report.dangling_blobs == {"rotten": ["payload"]}
    assert report.stale == ["old"]
    assert report.contract_violations == {"thin": ["id"]}


def test_the_cron_handler_stays_readable() -> None:
    """A cron tick that is one 100+-line body is the finding, not the style preference: lock handling,
    the sweep call, six partitions, an outbox drain, run retention, a WARN per finding class and two
    9-key mappings all read as one blob, and every one of them was reachable only through the route."""
    source = Path(inspect.getfile(reconcile_cron)).read_text(encoding="utf-8")
    handler = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_on_cron")
    span = (handler.end_lineno or handler.lineno) - handler.lineno + 1

    assert span <= 45, f"_on_cron is {span} lines — the sweep's steps belong in named functions the unit tier can drive"
