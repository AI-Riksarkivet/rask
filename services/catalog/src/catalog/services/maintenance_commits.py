"""The commits maintenance asks the catalog for, made under the catalog's own credential ([[LH-575]]).

Maintenance's vend deletes and puts nothing of a table (``catalog.core.vending``), so the two commits its passes need
are the catalog's, at maintainer doors the router judges like any other: the disarm of a ref's commit-path cleanup, the
backstop for a table no door commits on, and the raise of a table's listing floor. Each acts on the table its door
resolved (:class:`table_claims.DoorTable`).

A DISARM IS THE CATALOG'S OWN SHAPE. Every door's commit disarms its ref first, and a disarm removes
``lance.auto_cleanup.*`` keys and nothing else, so the commit reconciler counts no author for one
(:mod:`catalog.services.commit_reconcile`); the door's authorization is what records who asked.

A RAISE IS A STAMPED COMMIT (:func:`dataplane.commit_stamped`), so the catalog records its caller as the author of the
version (ADR 0187), as for any door's commit. The feed announces none, since it changes no row, so the reconciler asks
only that its author was recorded, and counts a raise that carries none.

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

import logging
import time
from typing import Final

from catalog.services import dataplane, table_claims
from lakehouse import auto_cleanup
from lakehouse.listing_floor import FLOOR_KEY
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: The ``rask.operation`` a floor raise is stamped with: a door's commit whose author is recorded and which the feed
#: announces nothing for.
RAISE_LISTING_FLOOR: Final = "raise_listing_floor"


def disarm(table: table_claims.DoorTable, so: StorageOptions, *, branch: str | None) -> tuple[int, list[str]]:
    """Remove every ``lance.auto_cleanup.*`` key from the ref ``branch`` (``None`` = main) names; return the ref's version
    after, and the keys removed. Commits nothing when no key is present (:func:`auto_cleanup.disarm`).

    Raises:
        TableChangedError: The table changed since the door's resolution.
    """
    dataset = table.open(so, branch=branch)
    removed = auto_cleanup.disarm(dataset)
    if removed:
        log.warning("auto_cleanup_disarmed", extra={"table": table.segments, "branch": branch, "keys": removed, "version": dataset.version})
    return int(dataset.version), removed


def raise_listing_floor(table: table_claims.DoorTable, so: StorageOptions, *, transaction_properties: dict[str, str]) -> tuple[int, float]:
    """Commit :data:`FLOOR_KEY` on main, stamped with ``transaction_properties``; return the version and the epoch the
    stamp names.

    The stamp is this pod's clock, read before the commit whose manifest this pod's clock times, so the floor reaches it
    once the versions beneath the raise are cleaned.

    Raises:
        ConcurrentModificationError: Every attempt lost its race.
        TableChangedError: The table changed since the door's resolution.
    """
    raised_at = time.time()
    version, _ = dataplane.commit_stamped(
        table, so, transaction_properties, dataplane.captured(table, so, lambda handle: handle.update_config({FLOOR_KEY: str(raised_at)}), branch=None)
    )
    log.info("listing_floor_raised", extra={"table": table.segments, "version": version, "raised_at": raised_at})
    return version, raised_at
