"""A table's or a namespace's deletion-protection record, as the catalog's doors read it."""

from __future__ import annotations

from lance_namespace import InternalError

from catalog.core.config import CatalogStore
from lakehouse import protection
from lakehouse.protection import ProtectedKind, ProtectionRecord


def read(settings: CatalogStore, kind: ProtectedKind, uuid: str) -> ProtectionRecord | None:
    """The protection record of the ``kind`` object ``uuid`` names, or ``None`` when none is filed (unprotected).

    Raises:
        InternalError: A record is filed for the object and cannot be read, so whether it is protected is unknown.
        OSError: The store failed the read.
    """
    try:
        return protection.get_protection(settings.registry_root, settings.storage_options(), kind, uuid)
    except protection.UnreadableProtectionRecordError as exc:
        raise InternalError(str(exc)) from exc
