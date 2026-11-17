"""Whether each warehouse's store enforces the seal, exported for the alert that fires while one does not (ADR 0242 B4).

The warehouse probe's seal check measures each warehouse's store at registration and on validate, and the record keeps
what it measured (``sealed``). This process exports it as ``catalog.warehouse.seal``, one series per warehouse, 1, with
the warehouse (``lance.warehouse``), what was measured (``lance.seal``: ``enforced``, ``unenforced``, ``untaggable``)
and whether git requires it (``lance.seal_required``: ``false`` for a warehouse declared ``detection_only``). The
alert ``WarehouseSealUnenforced`` (``deploy/alerting/rules.yml``) fires while any series is not ``enforced``.

Read from the registry at the catalog's start (:func:`load`) and updated by every registration and validate in this
process (:func:`observe`), so a restart keeps the series and a replica that ran no probe still reports every warehouse.
"""

from __future__ import annotations

import logging
from typing import Final

from opentelemetry import metrics

from catalog.core.config import Settings
from catalog.services import warehouses


log = logging.getLogger(__name__)

_meter = metrics.get_meter("lance.catalog")
_WAREHOUSE: Final = "lance.warehouse"
_SEAL: Final = "lance.seal"
_REQUIRED: Final = "lance.seal_required"

#: Each warehouse's measured seal and whether git requires it, as this process last read or measured it.
_postures: dict[str, tuple[str, bool]] = {}
_registered = False


def _observe(_options: metrics.CallbackOptions) -> list[metrics.Observation]:
    return [
        metrics.Observation(1, {_WAREHOUSE: warehouse, _SEAL: sealed, _REQUIRED: "true" if required else "false"})
        for warehouse, (sealed, required) in sorted(_postures.items())
    ]


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


def observe(warehouse_id: str, sealed: str | None, *, required: bool) -> None:
    """What a registration or a validate measured of ``warehouse_id``'s store; nothing for a check that never ran."""
    _register()
    if sealed is not None:
        _postures[warehouse_id] = (sealed, required)


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
        if (sealed := record.get(warehouses.SEALED)) is not None:
            _postures[record["id"]] = (sealed, record.get("bucket") not in detection_only)
