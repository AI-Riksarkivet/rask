"""Re-assert the cascade's grants over every warehouse that already exists.

WHY THIS EXISTS. `seed_warehouse` writes the cascade-writer tuples exactly once, when a warehouse is
created, from `LANCE_FGA_CASCADE_WRITERS` — a value that CHANGES. Adding a mover to
`medallion.movers` extends that list, and every warehouse created before the change is missing the
new subject. The model makes `can_update_tag: owner`, so the new mover cannot promote into any
existing tenant: rows land, lineage records the run, and the promotion is refused 403 in a log nobody
reads. Measured on the k3s estate 2026-08-23 — `user:service-silver-to-gold` held `owner` on neither
`warehouse:acme-bucket` nor `warehouse:research-bucket`, both created before the setting existed.

WHY A JOB AND NOT A STARTUP HOOK. The catalog's lifespan writes no tuples today, and making boot
depend on an OpenFGA write has no good failure branch: crash-loop the catalog because a grant could
not be re-asserted, or swallow it and boot with the drift still there. It would also run on every
replica and every rollout, racing itself. A post-upgrade hook runs once, at exactly the moment the
writer list can change, and fails visibly.

WHY NOT RE-POST /v1/warehouses. A same-id create converges the record and calls `seed_warehouse`
unconditionally, so it *would* land the tuples — while granting the CALLER `owner` on every tenant it
touched, re-provisioning each bucket, and re-emitting `warehouse_created`. It also needs
`can_create_warehouse` on every project. Repair must not require becoming an owner of what you repair.

ENUMERATION IS FROM THE REGISTRY, NOT FROM OPENFGA. Asking OpenFGA for "every warehouse" is not
possible: its Read API rejects a type-only object filter ("the object type field is required and both
the object id and user cannot be empty"), and ListObjects answers per-user, which is the question this
job is trying to fix the answer to. The registry under `LANCE_REST_ROOT` is the authoritative list.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING

from catalog.api import fga_deps
from catalog.core.config import get_settings
from catalog.services import warehouses
from service_kit.governed.auth_lifespan import build_fga_client


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient

    from catalog.core.config import Settings


log = logging.getLogger(__name__)

#: The actor recorded on every tuple this writes. Not a person: nobody authorised each individual
#: grant, the deployment did — and an audit row naming a human who merely ran `helm upgrade` would be
#: a worse lie than one naming the mechanism.
ACTOR = "system:cascade-backfill"


async def backfill(settings: Settings) -> tuple[int, int, list[str]]:
    """Re-assert the cascade grants for every registered warehouse.

    Returns ``(warehouses_seen, tuples_submitted, failures)``. A warehouse whose record is missing a
    `project` is SKIPPED and reported rather than guessed at: the project edge is what makes the whole
    concentric cascade resolve, and writing it wrong would grant a tenant's warehouse to the wrong
    tenant — strictly worse than leaving it unreachable.
    """
    if not settings.fga_enabled:
        log.info("cascade_backfill_skipped", extra={"reason": "fga_disabled"})
        return (0, 0, [])
    if not settings.fga_cascade_writers:
        # Not an error: an estate with no movers configured has nothing to grant. Logged because a
        # silent zero is the same shape as a broken run.
        log.info("cascade_backfill_skipped", extra={"reason": "no_cascade_writers"})
        return (0, 0, [])

    # THE SAME BOOTSTRAP THE LIFESPAN USES, not a private copy of it — this is a hook Job that must
    # land on the store `main.py` already resolved, and a second implementation is how a resolver and
    # a provisioner end up on different stores. `fatal=True` because the alternative here is
    # `_reconcile(None, …)`: a hook that cannot build a client has nothing to repair with, and its
    # caller (`main.py::_backfill_cascade_grants`) already catches and reports.
    client = await build_fga_client(settings, service="cascade-backfill", fatal=True)
    if client is None:  # unreachable while fga_enabled is checked above; narrows the Optional for `ty`
        raise RuntimeError("cascade backfill: FGA is enabled but no client could be built")
    try:
        return await _reconcile(client, settings)
    finally:
        # The catalog's own lifespan closes its client (main.py:191); a Job that does not leaves
        # "Unclosed client session" on the way out, which in a hook pod reads as a crash.
        await client.close()


async def _reconcile(client: OpenFgaClient, settings: Settings) -> tuple[int, int, list[str]]:
    records = await asyncio.to_thread(warehouses.list_warehouses, settings.registry_root, settings.storage_options())
    seen = written = 0
    failures: list[str] = []
    for record in records:
        warehouse_id, project = record.get("id"), record.get("project")
        if not warehouse_id:
            continue
        seen += 1
        if not project:
            failures.append(f"{warehouse_id}: record has no project")
            log.warning("cascade_backfill_no_project", extra={"warehouse": warehouse_id})
            continue
        try:
            written += await fga_deps.backfill_cascade_grants(client, settings, warehouse_id=warehouse_id, project=project, actor=ACTOR)
        except Exception as exc:
            # One unreachable warehouse must not abandon the rest — but it MUST be reported, and the
            # exit code must reflect it. A backfill that half-ran and said nothing is how an estate
            # ends up believing it is repaired.
            failures.append(f"{warehouse_id}: {exc}")
            log.warning("cascade_backfill_failed", extra={"warehouse": warehouse_id, "error": str(exc)})
    return (seen, written, failures)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    seen, written, failures = asyncio.run(backfill(get_settings()))
    log.info("cascade_backfill_done", extra={"warehouses": seen, "tuples": written, "failures": len(failures)})
    print(f"cascade-backfill: {seen} warehouses, {written} tuples submitted, {len(failures)} failed")
    for line in failures:
        print(f"  FAILED {line}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
