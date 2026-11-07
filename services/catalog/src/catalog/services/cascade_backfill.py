"""Converge the warehouse grants over every warehouse that already exists.

WHY THIS EXISTS. `seed_warehouse` writes the project edge and the maintainer tuples exactly once, when a
warehouse is created, from `LANCE_FGA_MAINTAINERS` — a value that CHANGES — so every warehouse created
before a change is missing the new subject, and the sweep cannot maintain it; and one of the release's own accounts the
change dropped keeps its rung until the backfill takes it back ([[XC-164]]).

WHY AT BOOT, IN THE BACKGROUND. The catalog runs it beside its lifespan (`main.py::_backfill_cascade_grants`), not
awaited and not fatal: a slow OpenFGA must not delay readiness, and a grant that could not be converged leaves the
estate as it was. A hook Job cannot do it, because the registry read needs the store's secret from the secret store,
which needs a Dapr sidecar, and a sidecar keeps a Job from completing. Every replica runs it; each write and each revoke
is idempotent (a revoke a sibling made first is recorded absent), so replicas racing converge on the same tuples.

WHY NOT RE-POST /management/v1/warehouses. A same-id create converges the record and calls `seed_warehouse`
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
from lakehouse.governed.auth_lifespan import build_fga_client


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient

    from catalog.core.config import Settings


log = logging.getLogger(__name__)

#: The actor recorded on every tuple this writes. Not a person: nobody authorised each individual
#: grant, the deployment did — and an audit row naming a human who merely ran `helm upgrade` would be
#: a worse lie than one naming the mechanism.
ACTOR = "system:cascade-backfill"


async def backfill(settings: Settings) -> tuple[int, int, list[str]]:
    """Converge the cascade grants of every registered warehouse.

    Returns ``(warehouses_seen, tuples_converged, failures)``, the tuples counted as written plus revoked. A warehouse
    whose record is missing a
    `project` is SKIPPED and reported rather than guessed at: the project edge is what makes the whole
    concentric cascade resolve, and writing it wrong would grant a tenant's warehouse to the wrong
    tenant — strictly worse than leaving it unreachable.
    """
    if not settings.fga_enabled:
        log.info("cascade_backfill_skipped", extra={"reason": "fga_disabled"})
        return (0, 0, [])
    if not (settings.fga_maintainers or settings.own_accounts):
        # Not an error: an estate that declares no maintenance identity and names no account of its own has nothing to
        # grant and nothing to take back. Logged because a silent zero is the same shape as a broken run.
        log.info("cascade_backfill_skipped", extra={"reason": "nothing_declared"})
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
            written += await fga_deps.converge_cascade_grants(client, settings, warehouse_id=warehouse_id, project=project, actor=ACTOR)
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
    print(f"cascade-backfill: {seen} warehouses, {written} tuples written or revoked, {len(failures)} failed")
    for line in failures:
        print(f"  FAILED {line}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
