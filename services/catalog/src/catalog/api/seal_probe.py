"""The tick that measures, for every warehouse the catalog serves, whether its store enforces the seal (ADR 0242).

A registration and ``POST /management/v1/warehouses/{id}/validate`` probe one warehouse when someone asks; the estate's
own warehouse has no registration, and a store can stop enforcing the seal at any time after one, so neither alone gives
``WarehouseSealLost`` a signal that moves on its own. So the catalog probes every warehouse it serves, the estate's own
among them, at its start (``catalog.main.converge_at_boot``, which no readiness waits on) and on the period git gives
this timer
(``catalog.sealProbe.periodSeconds``, ``LANCE_SEAL_PROBE_PERIOD_SECONDS``), once a window whatever the replica count
(`catalog.api.timers`): GitOps, the period is the chart's and nothing is hand-run.

Each probe is the one registration and validate make (``endpoints.warehouses.probe``) under the catalog's own subject,
and its verdict is kept as validate keeps it: a registered warehouse's on its record, the estate's on a record of its
own (``warehouses.set_estate_sealed``), each exported on ``catalog.warehouse.seal`` (``catalog.services.seal_posture``).
A probe that learned nothing (the store did not answer, refused nothing and admitted nothing, ADR 0242 § Found by review,
second round) leaves the record as it was, and the time it was measured with it, and is counted on
``catalog.warehouse.seal_unprobed``, so a store hiccup neither admits a store nor pages for one that still enforces; a
warehouse whose probe stays inconclusive for two periods pages on that time's age (``WarehouseSealUnmeasured``, § Found
by review, third round, B2).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from opentelemetry import metrics
from pydantic import BaseModel

from catalog.api.dependencies import get_vendor
from catalog.api.ticks import Tick
from catalog.api.v1.endpoints.warehouses import probe
from catalog.core.config import Settings
from catalog.core.vending import role_session_name
from catalog.services import seal_posture, warehouses


log = logging.getLogger(__name__)

#: The subject the probe's vends are made for: the catalog itself, named so on the store's own trail.
PROBE_SUBJECT = "rask-catalog-seal-probe"

_unprobed = metrics.get_meter("lance.catalog").create_counter(
    "catalog.warehouse.seal_unprobed",
    unit="{probe}",
    description="Seal probes of a warehouse's store that learned nothing (the store refused nothing and admitted nothing, or "
    "did not answer): the warehouse keeps the seal it last measured.",
)


class SealProbeTick(BaseModel):
    """What one tick measured."""

    #: Warehouses whose seal was measured and recorded.
    measured: int = 0
    #: Warehouses whose probe learned nothing.
    unprobed: int = 0


def create_series() -> None:
    """The unlabelled counter's series at 0 from the catalog's start, so its first count is an increase an alert reads."""
    _unprobed.add(0)


async def tick(ctx: Tick) -> SealProbeTick:
    """Probe the seal of every warehouse the catalog serves."""
    return await probe_all(ctx.app, ctx.settings)


async def probe_all(app: FastAPI, settings: Settings) -> SealProbeTick:
    """Probe the seal of the estate's own warehouse and of every registered one on the estate's store, and keep each
    verdict; a warehouse on another store is not the estate's to probe (``validated_ids`` admits none)."""
    vendor = get_vendor(Tick(app, settings).request)
    so = settings.storage_options()
    records = await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, so)
    served = [(settings.estate_warehouse, settings.root, None)] + [(str(record["id"]), str(record["root_uri"]), record) for record in records]
    done = SealProbeTick()
    for warehouse_id, root_uri, record in served:
        if not (bucket := seal_posture.probe_serves(settings, root_uri, None if record is None else record.get("endpoint"))):
            continue
        try:
            report = await probe(settings, vendor, root_uri, session_name=role_session_name(PROBE_SUBJECT))
        except Exception as exc:  # noqa: BLE001 — one warehouse this tick cannot probe must not stop the others
            report = None
            log.warning("warehouse_seal_unprobed", extra={"warehouse": warehouse_id, "error": f"{type(exc).__name__}: {exc}"[:300]})
        if report is None or report.sealed is None:
            _unprobed.add(1)
            done.unprobed += 1
            seal_posture.unmeasured(warehouse_id)
            if report is not None:
                log.warning("warehouse_seal_unprobed", extra={"warehouse": warehouse_id, "checks": [check.model_dump() for check in report.checks]})
            continue
        if record is None:
            measured = await run_in_threadpool(warehouses.set_estate_sealed, settings.registry_root, so, warehouse_id, report.sealed)
        else:
            measured = await run_in_threadpool(warehouses.set_warehouse_sealed, settings.registry_root, so, warehouse_id, report.sealed)
        seal_posture.observe(warehouse_id, measured, required=bucket not in settings.seal_detection_only_buckets)
        done.measured += 1
    log.info("warehouse_seal_probed", extra={"measured": done.measured, "unprobed": done.unprobed})
    return done
