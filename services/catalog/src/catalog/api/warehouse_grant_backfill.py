"""Converge the warehouse grants over every warehouse that already exists.

WHY THIS EXISTS. `seed_warehouse` writes the project edge and the maintainer tuples exactly once, when a
warehouse is created, from `LANCE_FGA_MAINTAINERS` — a value that CHANGES — so every warehouse created
before a change is missing the new subject, and maintenance cannot maintain it; and one of the release's own accounts the
change dropped keeps its rung until the backfill takes it back ([[XC-164]]).

WHY AT BOOT, IN THE BACKGROUND. The catalog runs it beside its lifespan (`main.py::_backfill_warehouse_grants`), not
awaited and not fatal, through the OpenFGA client the lifespan wired: a slow OpenFGA must not delay readiness, and a
grant that could not be converged leaves the estate as it was. Every replica runs it; each write and each revoke is idempotent (a revoke a sibling made first is
recorded absent), so replicas racing converge on the same tuples.

WHY NOT RE-POST /management/v1/warehouses. A same-id create converges the record and calls `seed_warehouse`
unconditionally, so it *would* land the tuples — while granting the CALLER `owner` on every tenant it
touched, re-provisioning each bucket, and re-emitting `warehouse_created`. It also needs
`can_create_warehouse` on every project. Repair must not require becoming an owner of what you repair.

ENUMERATION IS FROM THE REGISTRY, NOT FROM OPENFGA. Asking OpenFGA for "every warehouse" is not
possible: its Read API rejects a type-only object filter ("the object type field is required and both
the object id and user cannot be empty"), and ListObjects answers per-user, which is the question this
job is trying to fix the answer to. The registry under `LANCE_REST_ROOT` is the authoritative list.
"""

import logging
from typing import TYPE_CHECKING

from opentelemetry import metrics

from catalog.api.authz import warehouse_grants
from catalog.services import warehouses
from lakehouse.process.own_threads import on_own_threads


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient

    from catalog.core.config import Settings


log = logging.getLogger(__name__)

#: The actor recorded on every tuple this writes. Not a person: nobody authorised each individual
#: grant, the deployment did — and an audit row naming a human who merely ran `helm upgrade` would be
#: a worse lie than one naming the mechanism.
ACTOR = "system:warehouse-grant-backfill"

_failed = metrics.get_meter("lance.catalog").create_counter(
    "catalog.warehouse_grant_backfill.failed",
    unit="{warehouse}",
    description="Warehouses whose grants the catalog's boot backfill could not converge, each left as it was until the next boot.",
)


async def backfill(settings: Settings, client: OpenFgaClient | None) -> tuple[int, int, list[str]]:
    """Converge the warehouse grants of every registered warehouse through ``client``, the one the lifespan wired
    (``None`` where authorization is off).

    Returns ``(warehouses_seen, tuples_converged, failures)``, the tuples counted as written plus revoked. A warehouse
    whose grants could not be converged is reported in ``failures``, logged and counted, and the walk goes on.

    Raises:
        RuntimeError: Authorization is on and no client is wired: a catalog that did not start the way it serves.
    """
    if not settings.fga_enabled:
        log.info("warehouse_grant_backfill_skipped", extra={"reason": "fga_disabled"})
        return (0, 0, [])
    if client is None:
        raise RuntimeError("authorization is on and the lifespan wired no OpenFGA client")
    if not (settings.fga_maintainers or settings.own_accounts):
        # Not an error: an estate that declares no maintenance identity and names no account of its own has nothing to
        # grant and nothing to take back. Logged because a silent zero is the same shape as a broken run.
        log.info("warehouse_grant_backfill_skipped", extra={"reason": "nothing_declared"})
        return (0, 0, [])
    records = await on_own_threads(warehouses.list_warehouses, settings.registry_root, settings.storage_options())
    seen = written = 0
    failures: list[str] = []
    for record in records:
        warehouse_id, project = record.id, record.project
        seen += 1
        try:
            written += await warehouse_grants.converge_warehouse_grants(client, settings, warehouse_id=warehouse_id, project=project, actor=ACTOR)
        except Exception as exc:  # noqa: BLE001 — one warehouse that could not be converged must not abandon the rest
            failures.append(f"{warehouse_id}: {exc}")
            _failed.add(1)
            log.warning("warehouse_grant_backfill_failed", extra={"warehouse": warehouse_id, "error": str(exc)}, exc_info=exc)
    return (seen, written, failures)
