"""A governed table's name resolved to the uuids the catalog keys it by (ADR 0173 §10, [[LH-522]]).

A table's identity across a rename is its uuid, so lineage records and walks a governed table by it. The catalog stamps
the uuids on every event it emits; an event from an outside producer names its tables only, and lineage resolves those
names when it takes the event in, as it resolves the name a reader filters or walks by.

AS THE CALLER, never as this service, so a name reaches exactly the tables the caller may see and lineage holds no grant
to describe the estate. Where authorization is decided by the catalog, an event's names are asked about at its decision
door in one question, as the caller's verified subject and on the rung DescribeTable asks: the catalog resolves each
name there and answers the uuids of a table the caller may see ([[LH-531]]), and a name it refuses is one DescribeTable
would refuse the caller too. One question rather than a describe per name, so a producer that may see none of the
tables it names costs the catalog that question, however many it names. Elsewhere (authorization off, or decided from
the store, which holds no names) a name is resolved by the catalog's DescribeTable and DescribeNamespace under the
caller's own bearer. A name the catalog does not hold, or will not resolve for this caller, resolves to no uuid. What
the catalog refuses the name itself with (it is no name, the table changed under the read) is the caller's answer. Any
other failure is the outage: an event recorded under a name it should have been keyed by a uuid would split the table's
lineage, so the producer's retry is the better answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

from lance_namespace import ErrorCode
from pydantic import BaseModel, ConfigDict

from lakehouse.events.lineage_events import DatasetUuids, with_dataset_uuids
from lakehouse.governed.decider import AccessDecider
from lakehouse.naming import CATALOG_DELIMITER, identifier_location, uuid_of
from lakehouse.problems.ns_errors import status_for
from lineage.core.config import LineageSettings
from lineage.core.sources import is_external_source
from lineage.models import Dataset, DatasetEvent, RunEvent
from lineage.services.catalog_client import CATALOG_CALLS, catalog_read_client, request_headers
from lineage.services.catalog_reader import CatalogAnsweredError, logged_outage


if TYPE_CHECKING:
    from lance_namespace_urllib3_client.exceptions import ApiException


#: The codes of the catalog's refusals that mean "no object the caller may see by this name": the name names none, the
#: caller may not see it, or the catalog does not take the caller's credential (a token minted for lineage's audience,
#: whose names the decision door resolves). Read by the code, which identifies an error across every Lance namespace
#: (`lance_docs/ns_catalog/namespace/operations/errors.md`).
_UNSEEN: Final = frozenset({ErrorCode.NAMESPACE_NOT_FOUND, ErrorCode.TABLE_NOT_FOUND, ErrorCode.PERMISSION_DENIED, ErrorCode.UNAUTHENTICATED})
#: The codes of the other refusals a describe owes its caller, which lineage passes on: the name is no name, the table
#: changed under the read, or it is in no state to be described.
_RELAYED: Final = frozenset({ErrorCode.INVALID_INPUT, ErrorCode.CONCURRENT_MODIFICATION, ErrorCode.INVALID_TABLE_STATE})


def _unseen(refused: CatalogAnsweredError) -> bool:
    """Whether the catalog's answer says the caller may see no object by the name it asked for."""
    return refused.code in _UNSEEN


class _CatalogProblem(BaseModel):
    """What lineage reads of a refusal the catalog wrote: the spec's required ``code`` and the refusal's ``detail``."""

    model_config = ConfigDict(frozen=True)

    code: int
    detail: str


def _catalog_refusal(exc: ApiException) -> CatalogAnsweredError | None:
    """The refusal ``exc`` holds that a describe owes its caller, or ``None`` when the catalog's answer is none.

    Such a refusal is a 4xx whose body is a problem body carrying one of the codes above at the status the spec maps that
    code to, so lineage answers the catalog's own status and code or the outage, never a status of its own for the
    catalog's code. Any other answer says nothing about the caller's name: a 5xx or another body may come from whatever
    stands in front of the catalog, a throttle is no judgement of the name, and code 0 is the catalog serving no such
    operation, which is lineage's address for the catalog, or its request, being wrong.
    """
    status = exc.status or 0
    if not 400 <= status < 500 or exc.body is None:
        return None
    try:
        problem = _CatalogProblem.model_validate_json(exc.body)
        code = ErrorCode(problem.code)
    except ValueError:
        return None
    if code not in _UNSEEN | _RELAYED or status_for(code) != status:
        return None
    return CatalogAnsweredError(problem.detail, code=code)


@contextmanager
def _as_the_caller() -> Generator[None]:
    """Relay what the catalog refuses a describe asked under the caller's own bearer with.

    A refusal a describe owes its caller is the caller's to read, so it keeps the catalog's status, ``code`` and
    ``detail`` (:func:`_catalog_refusal`). Any other answer is the outage, logged and never relayed
    (`catalog_reader.logged_outage`). Kept in this module, the one place lineage asks the catalog as its caller: every
    call it makes as itself answers the outage alone (`catalog_reader.translate_catalog_errors`).
    """
    from lance_namespace_urllib3_client import exceptions as _api_exc  # optional dep

    try:
        yield
    except _api_exc.ApiException as exc:
        refusal = _catalog_refusal(exc)
        if refusal is None:
            raise logged_outage(exc) from exc
        raise refusal from exc


#: The rung the catalog's DescribeTable asks, which a name is resolved on at the decision door: a caller resolves there
#: exactly the tables it could describe.
_DESCRIBE_RELATION: Final = "can_get_metadata"


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
        return await CATALOG_CALLS.run(self._table, name, bearer)

    async def namespace(self, name: str, *, bearer: str | None) -> str | None:
        """The uuid of the namespace ``name`` names now, or ``None`` when it names none the caller may see."""
        return await CATALOG_CALLS.run(self._namespace, name, bearer)

    def _table(self, name: str, bearer: str | None) -> DatasetUuids:
        from lance_namespace_urllib3_client.api.table_api import TableApi
        from lance_namespace_urllib3_client.models import DescribeTableRequest

        try:
            with _as_the_caller():
                described = TableApi(catalog_read_client(self._url)).describe_table(
                    # The uuid is all this reads, so no credential is minted for the caller's location.
                    name,
                    DescribeTableRequest(vend_credentials=False),
                    delimiter=CATALOG_DELIMITER,
                    _request_timeout=self._timeout,
                    _headers=request_headers(bearer),
                )
        except CatalogAnsweredError as refused:
            if not _unseen(refused):
                raise
            return DatasetUuids()
        table_uuid = uuid_of(described.properties)
        parent = name.rpartition(CATALOG_DELIMITER)[0]
        return DatasetUuids(table_uuid=table_uuid, namespace_uuid=self._namespace(parent, bearer) if parent and table_uuid else None)

    def _namespace(self, name: str, bearer: str | None) -> str | None:
        from lance_namespace_urllib3_client.api.namespace_api import NamespaceApi
        from lance_namespace_urllib3_client.models import DescribeNamespaceRequest

        try:
            with _as_the_caller():
                described = NamespaceApi(catalog_read_client(self._url)).describe_namespace(
                    name, DescribeNamespaceRequest(), delimiter=CATALOG_DELIMITER, _request_timeout=self._timeout, _headers=request_headers(bearer)
                )
        except CatalogAnsweredError as refused:
            if not _unseen(refused):
                raise
            return None
        return uuid_of(described.properties)


#: The ``producer`` of the uuids lineage stamps on an outside producer's datasets.
_PRODUCER: Final = "https://github.com/AI-Riksarkivet/rask/tree/main/services/lineage/src/lineage/services/names.py"


async def stamp_resolved_uuids(
    event: RunEvent | DatasetEvent,
    names: CatalogNames,
    *,
    bearer: str | None,
    decider: AccessDecider | None = None,
    subject: str | None = None,
) -> None:
    """Name every governed dataset ``event`` names by the uuids its name resolves to for the caller, and no other.

    For the HTTP door, whose caller holds a bearer and no signing key: a uuid in its body is a claim, as a signature
    there is, so each governed dataset's is replaced by what the catalog answers and an external source's is removed.
    The names are asked about in one question through ``decider`` as ``subject``, the caller the bearer was verified as,
    when both are given and the decider resolves names; otherwise each is described under ``bearer``. A dataset whose
    name resolves to no table keeps none, and is recorded by its name. A dataset named by a storage location a governed table holds is named by that table's uuid, as
    the catalog's ``locate`` door answers it through ``decider`` ([[LH-553]]): it is that table, which the graph the feed
    is forwarded to joins it to by the table's ``symlinks``, and `fga_deps.enforce_output_authz` decides it on its grants.
    """
    datasets = [*event.inputs, *event.outputs]
    governed = sorted({dataset.name for dataset in datasets if not is_external_source(dataset.namespace, dataset.name)})
    if decider is not None and subject is not None and decider.resolves_names:
        decided = await decider.check_tables(user=subject, relation=_DESCRIBE_RELATION, tables=governed) if governed else {}
        resolved = {name: DatasetUuids(table_uuid=d.table_uuid, namespace_uuid=d.namespace_uuid) for name, d in decided.items()}
    else:
        resolved = dict(zip(governed, await asyncio.gather(*(names.table(name, bearer=bearer) for name in governed)), strict=True))
    located = await _located(datasets, decider) if decider is not None else {}
    for dataset in datasets:
        if is_external_source(dataset.namespace, dataset.name):
            location = identifier_location(dataset.namespace, dataset.name)
            uuids = DatasetUuids(table_uuid=located.get(location)) if location is not None else DatasetUuids()
        else:
            uuids = resolved[dataset.name]
        dataset.facets = with_dataset_uuids({"facets": dataset.facets}, uuids, producer=_PRODUCER).get("facets", {})


async def _located(datasets: list[Dataset], decider: AccessDecider) -> dict[str, str | None]:
    """The uuid of the governed table each storage location ``datasets`` are named by holds, as the catalog answers it."""
    locations = sorted(
        {location for d in datasets if is_external_source(d.namespace, d.name) and (location := identifier_location(d.namespace, d.name)) is not None}
    )
    return await decider.locate(locations) if locations else {}
