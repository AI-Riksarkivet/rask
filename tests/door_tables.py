"""The table a catalog door hands its handler, built without a request for a test that drives the handler's service."""

from __future__ import annotations

from collections.abc import Callable

from lance_namespace import LanceNamespace

from catalog.services.table_claims import DoorTable, resolve
from lakehouse.location_claims import ClaimStore


def door_table(ns: LanceNamespace, store: ClaimStore, segments: list[str], *, announces: Callable[[str], bool]) -> DoorTable:
    """The table ``segments`` names now, whose writes the emitter judges by ``announces``."""
    return DoorTable(ns, store, resolve(ns, store, segments), announces=announces)
