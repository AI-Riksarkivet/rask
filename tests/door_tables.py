"""The table a catalog door hands its handler, built without a request for a test that drives the handler's service."""

from collections.abc import Callable
from typing import Final

from lance_namespace import LanceNamespace

from catalog.core.config import Settings
from catalog.services.table_claims import DoorTable, commit_filer, resolve
from lakehouse.records.location_claims import ClaimStore


#: The Arrow body cap a write door's handler is handed: the catalog's own default, read off its setting.
BODY_LIMIT: Final[int] = Settings.model_fields["max_body_bytes"].default


def door_table(ns: LanceNamespace, store: ClaimStore, segments: list[str], *, announces: Callable[[str], bool]) -> DoorTable:
    """The table ``segments`` names now, whose writes the emitter judges by ``announces``."""
    return DoorTable(ns, store, resolve(ns, store, segments), announces=announces, file_commit=commit_filer(store))
