"""Uuid readers that resolve nothing, for a test that calls a door's function directly rather than through a request.

A door names its objects by the uuids the catalog minted (ADR 0173 §10). Through a request the catalog reads them with
``catalog.api.dependencies.ObjectUuids`` and lineage with ``lineage.services.names.CatalogNames``; a test driving a door
whose claim is not about the uuids passes these, so its events name no uuid but one the door resolved itself.
"""

from __future__ import annotations

from typing import cast

from catalog.api.dependencies import ObjectUuids
from lineage.services.names import CatalogNames
from service_kit.control_events import ControlUuids
from service_kit.openlineage import DatasetUuids


class _NoObjectUuids:
    """``ObjectUuids``' whole signature, answering only the uuid a door passes in."""

    async def table(self, segments: list[str], *, location: str | None = None, uuid: str | None = None) -> DatasetUuids:
        return DatasetUuids(table_uuid=uuid)

    async def namespace(self, segments: list[str]) -> ControlUuids:
        return ControlUuids()

    async def namespace_of(self, name: str) -> str | None:
        return None


class _NoCatalogNames:
    """``CatalogNames``' whole signature: every name resolves to no uuid."""

    async def table(self, name: str, *, bearer: str | None) -> DatasetUuids:
        return DatasetUuids()

    async def namespace(self, name: str, *, bearer: str | None) -> str | None:
        return None


NO_OBJECT_UUIDS = cast(ObjectUuids, _NoObjectUuids())
NO_CATALOG_NAMES = cast(CatalogNames, _NoCatalogNames())
