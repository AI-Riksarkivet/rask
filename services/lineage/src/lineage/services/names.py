"""A governed table's name resolved to the uuids the catalog keys it by (ADR 0173 §10, [[LH-522]]).

A table's identity across a rename is its uuid, so lineage records and walks a governed table by it. The catalog stamps
the uuids on every event it emits; an event from an outside producer names its tables only, and lineage resolves those
names when it takes the event in, as it resolves the name a reader filters or walks by.

AS THE CALLER, never as this service: the resolution is the catalog's DescribeTable and DescribeNamespace with the
caller's own bearer, so it reaches exactly the tables the caller may see, and lineage holds no grant to describe the
estate. A name the catalog does not hold, or will not describe for this caller, resolves to no uuid. A service
account's projected token is audience-bound to lineage, so the catalog refuses it and such a caller's names stay
unresolved. Any other failure raises: an event recorded under a name it should have been keyed by a uuid would split
the table's lineage, so the producer's retry is the better answer.
"""

from __future__ import annotations

import asyncio
from typing import Final

from lineage.core.config import LineageSettings
from lineage.models import DatasetEvent, RunEvent
from service_kit.exceptions import ForbiddenError, NotFoundError, UnauthorizedError
from service_kit.lakehouse.naming import CATALOG_DELIMITER, is_external_source_namespace, uuid_of
from service_kit.lancekit.catalog_client import catalog_api_client, request_headers
from service_kit.lancekit.reader import translate_catalog_errors
from service_kit.openlineage import DatasetUuids, with_dataset_uuids


#: The catalog's answers that mean "no object the caller may see by this name": the name names none, the caller may
#: not see it, or the catalog does not take the caller's credential.
_UNSEEN: Final = (NotFoundError, ForbiddenError, UnauthorizedError)


class CatalogNames:
    """Resolves a governed table's or namespace's name to its uuid through the catalog's describe doors, as the caller."""

    def __init__(self, *, catalog_url: str, timeout_seconds: float = 30.0) -> None:
        self._url = catalog_url.rstrip("/")
        self._timeout = timeout_seconds

    @classmethod
    def from_settings(cls, settings: LineageSettings) -> CatalogNames:
        return cls(catalog_url=settings.catalog_url, timeout_seconds=settings.catalog_timeout_seconds)

    async def table(self, name: str, *, bearer: str | None) -> DatasetUuids:
        """The uuids of the table ``name`` names now and of its namespace; none when it names no table the caller may see."""
        return await asyncio.to_thread(self._table, name, bearer)

    async def namespace(self, name: str, *, bearer: str | None) -> str | None:
        """The uuid of the namespace ``name`` names now, or ``None`` when it names none the caller may see."""
        return await asyncio.to_thread(self._namespace, name, bearer)

    def _table(self, name: str, bearer: str | None) -> DatasetUuids:
        from lance_namespace_urllib3_client.api.table_api import TableApi
        from lance_namespace_urllib3_client.models import DescribeTableRequest

        try:
            with translate_catalog_errors():
                described = TableApi(catalog_api_client(self._url)).describe_table(
                    # The uuid is all this reads, so no credential is minted for the caller's location.
                    name,
                    DescribeTableRequest(vend_credentials=False),
                    delimiter=CATALOG_DELIMITER,
                    _request_timeout=self._timeout,
                    _headers=request_headers(bearer),
                )
        except _UNSEEN:
            return DatasetUuids()
        table_uuid = uuid_of(described.properties)
        parent = name.rpartition(CATALOG_DELIMITER)[0]
        return DatasetUuids(table_uuid=table_uuid, namespace_uuid=self._namespace(parent, bearer) if parent and table_uuid else None)

    def _namespace(self, name: str, bearer: str | None) -> str | None:
        from lance_namespace_urllib3_client.api.namespace_api import NamespaceApi
        from lance_namespace_urllib3_client.models import DescribeNamespaceRequest

        try:
            with translate_catalog_errors():
                described = NamespaceApi(catalog_api_client(self._url)).describe_namespace(
                    name, DescribeNamespaceRequest(), delimiter=CATALOG_DELIMITER, _request_timeout=self._timeout, _headers=request_headers(bearer)
                )
        except _UNSEEN:
            return None
        return uuid_of(described.properties)


#: The ``producer`` of the uuids lineage stamps on an outside producer's datasets.
_PRODUCER: Final = "https://github.com/AI-Riksarkivet/rask/tree/main/services/lineage/src/lineage/services/names.py"


async def stamp_resolved_uuids(event: RunEvent | DatasetEvent, names: CatalogNames, *, bearer: str | None) -> None:
    """Name every governed dataset ``event`` names by the uuids its name resolves to for the caller, and no other.

    For the HTTP door, whose caller holds a bearer and no signing key: a uuid in its body is a claim, as a signature
    there is, so each governed dataset's is replaced by what the catalog answers and an external source's is removed.
    A dataset whose name resolves to no table keeps none, and is recorded by its name.
    """
    datasets = [*event.inputs, *event.outputs]
    governed = sorted({dataset.name for dataset in datasets if not is_external_source_namespace(dataset.namespace, dataset.name)})
    resolved = dict(zip(governed, await asyncio.gather(*(names.table(name, bearer=bearer) for name in governed)), strict=True))
    for dataset in datasets:
        uuids = DatasetUuids() if is_external_source_namespace(dataset.namespace, dataset.name) else resolved[dataset.name]
        dataset.facets = with_dataset_uuids({"facets": dataset.facets}, uuids, producer=_PRODUCER).get("facets", {})
