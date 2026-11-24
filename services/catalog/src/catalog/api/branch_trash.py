"""The tick that deletes for good each hidden branch past its deadline ([[LH-429]]).

A recoverable branch delete hides the branch and keeps its bytes (`lakehouse.records.branch_trash`). This tick ends
that: for every record past its deadline it runs Lance's own branch delete at the table's location, releases the tag
held on the version the branch was cut from, and ends the record (`catalog.services.tables.hidden_branches.purge`),
the branches nested inside another first, as the delete door removes them. A branch a pass cannot delete (the store
did not answer, a ref write holds the table's history) keeps its record for the next pass, is logged and is counted on
``catalog.branch_trash.failed``.

It runs on the catalog's own timer (`catalog.api.timers`), once a window whatever the replica count.
"""

from __future__ import annotations

import logging
from functools import partial

from fastapi.concurrency import run_in_threadpool
from opentelemetry import metrics
from pydantic import BaseModel

from catalog.api.ticks import Tick
from catalog.services import table_claims
from catalog.services.tables import hidden_branches
from lakehouse.records import branch_trash


log = logging.getLogger(__name__)

_meter = metrics.get_meter("lance.catalog")
_purged = _meter.create_counter(
    "catalog.branch_trash.purged", unit="{branch}", description="Hidden branches deleted for good once past their deadline ([[LH-429]])."
)
_failed = _meter.create_counter(
    "catalog.branch_trash.failed",
    unit="{branch}",
    description="Hidden branches past their deadline a pass could not delete: each keeps its bytes and its record until a pass can.",
)


class BranchTrashTick(BaseModel):
    """What one tick did."""

    purged: int = 0
    failed: int = 0


async def tick(ctx: Tick) -> BranchTrashTick:
    """Delete for good every hidden branch whose deadline has passed."""
    store = table_claims.claim_store(ctx.settings)
    now = ctx.clock()
    hidden = await run_in_threadpool(branch_trash.of_every_table, store.control_root, store.storage_options)
    done = BranchTrashTick()
    for table_uuid, branches in hidden.items():
        for branch in sorted(branches, key=lambda kept: len(kept.name), reverse=True):
            if not branch.past_deadline(now):
                continue
            try:
                deleted = await run_in_threadpool(partial(hidden_branches.purge, store, ctx.storage_options, table_uuid, hidden=branch))
            except Exception as exc:  # noqa: BLE001 — one branch a pass cannot delete must not stop the others; the next pass tries it again
                log.error("hidden_branch_not_purged", extra={"table_uuid": table_uuid, "branch": branch.name, "error": str(exc)[:300]})
                _failed.add(1)
                done.failed += 1
                continue
            if deleted:
                _purged.add(1)
                done.purged += 1
    if done.purged or done.failed:
        log.info("branch_trash_tick", extra={"purged": done.purged, "failed": done.failed})
    return done
