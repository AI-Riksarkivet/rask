"""Whether each warehouse's store enforces the seal, and when that was last measured, exported for the alerts that fire
while one does not, or while nobody knows (ADR 0242 B4, § Found by review, third round, B2).

The warehouse probe's seal check measures each warehouse's store at registration, on validate and on the catalog's seal
probe, and the record keeps what it measured (``sealed``) and when (``sealed_at``). This process exports them:

- ``catalog.warehouse.seal``, one series per warehouse, 1, with the warehouse (``lance.warehouse``), what was measured
  (``lance.seal``: ``enforced``, ``unenforced``, ``untaggable``) and whether git requires it (``lance.seal_required``:
  ``false`` for a warehouse declared ``detection_only``). ``WarehouseSealUnenforced`` and ``WarehouseSealLost``
  (``deploy/alerting/rules.yml``) fire while a series is not ``enforced``.
- ``catalog.warehouse.seal_measured``, one series per warehouse, the Unix time of the last measurement that concluded.
  A probe that learned nothing moves neither series, so a store whose probe stays inconclusive keeps exporting the
  verdict it last had; ``WarehouseSealUnmeasured`` fires once that time is older than two of the probe's periods, which
  it reads off the period the probe's timer exports (``timer.period``, ``timer="catalog-seal-probe"``,
  :mod:`lakehouse.timers`), so a period git changes moves the alert with it. The time is the record's, read again at
  every start, so no restart and no counter reset makes an unmeasured warehouse look fresh.

Read from the registry at the catalog's start (:func:`load`), the estate's own warehouse from the record the catalog's
seal probe keeps for it, updated by every registration, validate and seal probe in this process (:func:`observe`), and
read again by a replica whose timer found the probe's window another replica's (:func:`load_in_background`), so a restart
keeps the series and every replica reports what the last probe measured, which no stale series of its own outlives.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Final

from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict

from catalog.core.config import Settings
from catalog.services import warehouses
from lakehouse.storage import split_s3_uri


log = logging.getLogger(__name__)

_meter = metrics.get_meter("lance.catalog")
_WAREHOUSE: Final = "lance.warehouse"
_SEAL: Final = "lance.seal"
_REQUIRED: Final = "lance.seal_required"


class _Posture(BaseModel):
    """One warehouse's measured seal, whether git requires it, and when it was measured."""

    model_config = ConfigDict(frozen=True)

    sealed: str
    required: bool
    #: ``None`` for a record that names no time it can be read as: it exports no ``seal_measured`` series.
    at: datetime | None


#: Each warehouse's posture as this process last read or measured it.
_postures: dict[str, _Posture] = {}
_registered = False


def _observe(_options: metrics.CallbackOptions) -> list[metrics.Observation]:
    return [
        metrics.Observation(1, {_WAREHOUSE: warehouse, _SEAL: posture.sealed, _REQUIRED: "true" if posture.required else "false"})
        for warehouse, posture in sorted(_postures.items())
    ]


def _observe_measured(_options: metrics.CallbackOptions) -> list[metrics.Observation]:
    return [metrics.Observation(posture.at.timestamp(), {_WAREHOUSE: warehouse}) for warehouse, posture in sorted(_postures.items()) if posture.at is not None]


def _register() -> None:
    global _registered
    if _registered:
        return
    _registered = True
    _meter.create_observable_gauge(
        "catalog.warehouse.seal",
        callbacks=[_observe],
        unit="{warehouse}",
        description=(
            "1 per warehouse, by what the probe's seal check last measured of its store (lance.seal: enforced, unenforced, "
            "untaggable) and whether git requires the seal (lance.seal_required). Any series not enforced is a warehouse "
            "whose committed files a write credential can replace with a 200."
        ),
    )
    _meter.create_observable_gauge(
        "catalog.warehouse.seal_measured",
        callbacks=[_observe_measured],
        unit="s",
        description="Per warehouse, the Unix time the probe's seal check last concluded (enforced, unenforced or untaggable); a probe that learned nothing leaves it.",
    )


def observe(warehouse_id: str, record: dict[str, Any] | None, *, required: bool) -> None:
    """What a registration, a validate or a seal probe recorded of ``warehouse_id``'s store (``record``, holding
    :data:`warehouses.SEALED` and :data:`warehouses.SEALED_AT`); nothing for a record that holds no measurement."""
    _register()
    if record is not None and isinstance(sealed := record.get(warehouses.SEALED), str):
        _postures[warehouse_id] = _Posture(sealed=sealed, required=required, at=warehouses.sealed_at(record))


def forget(warehouse_id: str) -> None:
    """A warehouse deleted: it exports no series from now on."""
    _postures.pop(warehouse_id, None)


def load(settings: Settings) -> None:
    """Every registered warehouse's measured seal, read off the registry: run at the catalog's start. A registry that does
    not answer is logged and leaves the series to the next registration or validate."""
    _register()
    try:
        records = warehouses.list_warehouses(settings.registry_root, settings.storage_options())
    except (OSError, ValueError) as exc:
        log.warning("seal_posture_unread", extra={"error": str(exc)[:300]})
        return
    detection_only = settings.seal_detection_only_buckets
    for record in records:
        observe(str(record["id"]), record, required=record.get("bucket") not in detection_only)
    try:
        estate = warehouses.estate_seal(settings.registry_root, settings.storage_options(), settings.estate_warehouse)
    except (OSError, ValueError) as exc:
        log.warning("seal_posture_unread", extra={"error": str(exc)[:300]})
        return
    observe(settings.estate_warehouse, estate, required=split_s3_uri(settings.root.rstrip("/"))[0] not in detection_only)


def load_in_background(settings: Settings) -> None:
    """:func:`load` on a thread of its own: what a replica runs on a seal probe's window another replica ran, from the
    timer's loop, which must not wait on the store."""
    threading.Thread(target=load, args=(settings,), name="seal-posture-load", daemon=True).start()
