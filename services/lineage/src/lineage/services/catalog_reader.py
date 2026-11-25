"""The catalog's read transport: a table read through the spec's REST doors.

:class:`CatalogTableReader` reads a table through ``POST /v1/table/{id}/query`` (a scan, paged and pinned to one
version), ``/count_rows``, ``describe`` (the current version) and ``/version/list``, over a :class:`CatalogTransport`;
:class:`RestCatalogTransport` is that transport over HTTP. The base URL comes from the caller, never a hard-coded host,
and the transport carries a urllib3 ``Retry``, so it runs behind a proxy unchanged.

The ``/query`` door is vector-search-first: it requires a ``vector`` and ``k`` and has no plain-scan verb, so a scan
is an EMPTY vector, a bounded ``k`` and the filter.
"""

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, Protocol

import pyarrow as pa
from lance_namespace import ErrorCode, LanceNamespaceError
from pydantic import BaseModel

from lakehouse.arrow_ipc import decode_arrow_response
from lakehouse.naming import CATALOG_DELIMITER
from lineage.services.catalog_client import catalog_read_client
from lineage.services.catalog_client import request_headers as _request_headers


log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from lance_namespace_urllib3_client import QueryTableRequest
    from lance_namespace_urllib3_client.exceptions import ApiException


#: What lineage answers in place of a catalog answer its caller may not read; the answer itself goes to the log.
_UNAVAILABLE: Final = "the catalog is unavailable"
#: How much of the catalog's answer the log keeps: a problem body whole, or the head of a proxy's page.
_LOGGED_BODY: Final = 512


class CatalogAnsweredError(LanceNamespaceError):
    """The catalog refused, or could not serve, a call lineage made through its doors.

    ``code`` is 17, the outage, for every answer to a call lineage made as itself (:func:`translate_catalog_errors`) and
    for any answer that is no refusal a describe owes its caller. It is the catalog's own only for such a refusal of a
    describe asked under the caller's own bearer, which is the caller's to read (`lineage.services.names`).

    One class rather than the spec's class for the code, so nothing of lineage's reads the catalog's answer as a
    refusal of lineage's own: its lanes park a ``PermissionDeniedError`` as lineage's refusal of an event, while a
    catalog 403 to lineage's own identity is an outage of lineage's grant there, which a delivery asks for again.
    """

    def __init__(self, message: str, *, code: ErrorCode = ErrorCode.SERVICE_UNAVAILABLE) -> None:
        super().__init__(message)
        self.code = code


class CatalogVersion(BaseModel):
    """One entry of the catalog's version listing (``/v1/table/{id}/version/list``):
    the Lance version number + its commit timestamp in epoch millis (``None`` when
    the backend records none)."""

    version: int
    timestamp_millis: int | None = None


class CatalogTransport(Protocol):
    """Executes the catalog reads :class:`CatalogTableReader` needs — ``/query``
    (Arrow IPC **file** bytes), ``/count_rows`` and ``/version/list``.

    Mirrors the calls the reader makes on the lance-ns ``DataApi``/``TableApi``, so the same reader runs over any
    transport that answers them as the catalog does.
    """

    def query(self, request: QueryTableRequest) -> bytes: ...

    def count(self, table_id: list[str], filter: str | None, *, version: int | None = None) -> int: ...

    def table_version(self, table_id: list[str]) -> int: ...

    def list_versions(self, table_id: list[str], *, limit: int | None = None) -> list[CatalogVersion]: ...


#: The bytes one page of a catalog read asks for: a quarter of the catalog's default 16 MiB answer
#: ceiling (`LANCE_QUERY_MAX_RESPONSE_MB`), so a page sized from a measured row width stays under it with
#: room for the catalog estimating that width differently ([[LH-247]]).
_PAGE_BYTES: Final = 4 << 20
#: The first page's row count, before any row width is known.
_FIRST_PAGE_ROWS: Final = 64


class CatalogTableReader:
    """Reads a table through the lance-ns catalog ``/query`` contract.

    ``table_id`` is the catalog identifier as a list (``[namespace, table]``). A scan is expressed as an
    empty vector (the endpoint has no plain-scan verb) and read in PAGES of a bounded ``k`` plus an
    ``offset``, every page pinned to one table version: the catalog refuses a ``k`` whose answer could
    exceed its ceiling, because it builds each answer whole in memory, so an unbounded logical read is
    many bounded answers, never one. Each page is :data:`_PAGE_BYTES` worth of rows at the width the last page
    of that projection measured, on this reader, so a reader making many small reads makes one query each; a
    projection it has not read yet starts at :data:`_FIRST_PAGE_ROWS` rows. A page shorter than the ``k`` it asked
    for is the last. Decodes each ``application/vnd.apache.arrow.file`` response with
    :func:`~lakehouse.arrow_ipc.decode_arrow_response`, which validates it in full and raises
    :class:`~lakehouse.arrow_ipc.ArrowBodyError` for bytes that are not a valid Arrow IPC body.
    """

    def __init__(self, transport: CatalogTransport, table_id: list[str]) -> None:
        self._transport = transport
        self._id = table_id
        #: The rows a page of each projection takes, from the width its last page measured.
        self._page_rows: dict[tuple[str, ...] | None, int] = {}

    def _build_request(
        self,
        *,
        columns: list[str] | None,
        filter: str | None,
        k: int,
        offset: int,
        with_row_id: bool,
        version: int,
    ) -> QueryTableRequest:
        from lance_namespace_urllib3_client import (  # optional dep: catalog backend only
            QueryTableRequest,
            QueryTableRequestColumns,
            QueryTableRequestVector,
        )

        return QueryTableRequest(
            id=self._id,
            # empty vector ⇒ scan intent (no plain-scan verb exists)
            vector=QueryTableRequestVector(single_vector=[]),
            k=k,
            columns=QueryTableRequestColumns(column_names=columns) if columns else None,
            filter=filter,
            offset=offset or None,
            with_row_id=with_row_id or None,
            version=version,
        )

    def to_table(
        self,
        *,
        columns: list[str] | None = None,
        filter: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        with_row_id: bool = False,
        version: int | None = None,
    ) -> pa.Table:
        """Scan the table, page by page — ``version`` pins a HISTORICAL snapshot (catalog time-travel;
        a bad/reclaimed version is the catalog's 404, raised as the outage a :class:`CatalogAnsweredError` is), and an
        unpinned read pins the version current when it starts, so its pages cannot straddle a commit.
        """
        pinned = version if version is not None else self.table_version()
        start = offset or 0
        pages: list[pa.Table] = []
        read = 0
        projection = tuple(columns) if columns else None
        page_rows = self._page_rows.get(projection, _FIRST_PAGE_ROWS)
        while True:
            k = max(1, page_rows if limit is None else min(page_rows, limit - read))
            request = self._build_request(columns=columns, filter=filter, k=k, offset=start + read, with_row_id=with_row_id, version=pinned)
            # The response is bytes from another service, validated in full like a caller's body, because
            # framing that parses says nothing about the buffers it frames. It is not held to its size as a
            # body is: the query bounds a response, and an honest projection can be as dense as a hostile body.
            page = decode_arrow_response(self._transport.query(request))
            pages.append(page)
            read += page.num_rows
            if page.num_rows:
                # At least a byte a row: a projection of a field that stores nothing measures none.
                page_rows = max(1, _PAGE_BYTES // max(1, -(-page.nbytes // page.num_rows)))
                self._page_rows[projection] = page_rows
            if page.num_rows < k or (limit is not None and read >= limit):
                break
        table = pa.concat_tables(pages)
        return table if limit is None else table.slice(0, limit)

    def count_rows(self, filter: str | None = None, *, version: int | None = None) -> int:
        return self._transport.count(self._id, filter, version=version)

    def table_version(self) -> int:
        return self._transport.table_version(self._id)

    def versions(self, *, limit: int | None = None) -> list[CatalogVersion]:
        """The table's version history, latest first (the catalog's ``descending``
        listing), capped at ``limit`` — the surface the compare-versions panel reads."""
        return self._transport.list_versions(self._id, limit=limit)

    @property
    def schema(self) -> pa.Schema:
        return self.to_table(limit=0).schema


def logged_outage(exc: ApiException) -> CatalogAnsweredError:
    """The outage lineage answers in place of ``exc``, the catalog's answer, which is logged here: an ingress's HTML page,
    a diagnostic or a refusal of lineage's own identity, it goes to the operator and never to the wire.

    A warning carrying the answer's status, reason and the head of its body, with no traceback: the failure itself is
    logged by whatever handles the outage, which holds ``exc`` as its cause.
    """
    log.warning("catalog_unavailable", extra={"status": exc.status, "reason": exc.reason, "body": (exc.body or "")[:_LOGGED_BODY]})
    return CatalogAnsweredError(_UNAVAILABLE)


@contextmanager
def translate_catalog_errors() -> Generator[None]:
    """Answer the failure of a call lineage made through the generated client AS ITSELF as the outage, whatever the
    catalog said.

    Lineage reads and writes its provenance tables under its own identity, so what the catalog refuses there is lineage's
    own grant, token, rows or tables: nothing lineage's caller asked for, and nothing it may read. The caller is answered
    code 17 with a stable sentence and the catalog's answer goes to the log (:func:`logged_outage`). The client's
    exception is the cause, carrying the HTTP status. A describe asked under the caller's own bearer relays the
    catalog's refusal instead (`lineage.services.names`).
    """
    from lance_namespace_urllib3_client import exceptions as _api_exc  # optional dep

    try:
        yield
    except _api_exc.ApiException as exc:
        raise logged_outage(exc) from exc


class RestCatalogTransport:
    """Live REST catalog transport — ``POST /v1/table/{id}/query`` over HTTP.

    Host-agnostic (base URL from settings, never hard-coded) + retry-friendly
    (urllib3 ``Retry`` via the generated ``Configuration``) so it drops into a
    proxy-fronted catalog unchanged. Exercised against the catalog app over HTTP by
    ``tests/integration/test_a_catalog_reader_reads_a_whole_table_in_bounded_pages.py``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        delimiter: str = CATALOG_DELIMITER,
        token: str | None = None,
        retries: int = 3,
        timeout: float = 30.0,
    ) -> None:
        from lance_namespace_urllib3_client.api.data_api import DataApi

        # ONE client (and so one connection pool) per catalog, shared with every other transport
        # aimed at it — this ctor may run once per request.
        self._api = DataApi(catalog_read_client(base_url, retries=retries))
        self._token = token
        self._delimiter = delimiter
        self._timeout = timeout

    def request_headers(self) -> dict[str, str]:
        """THIS caller's bearer, per request — never a default header on the shared client."""
        return _request_headers(self._token)

    def query(self, request: QueryTableRequest) -> bytes:
        id_str = self._delimiter.join(request.id or [])
        with translate_catalog_errors():
            raw = self._api.query_table(id_str, request, delimiter=self._delimiter, _request_timeout=self._timeout, _headers=self.request_headers())
        return bytes(raw)

    def count(self, table_id: list[str], filter: str | None, *, version: int | None = None) -> int:
        from lance_namespace_urllib3_client import CountTableRowsRequest

        id_str = self._delimiter.join(table_id)
        # ``version`` pins the count at a historical snapshot (spec 0.9; the native
        # backend checks out that version server-side) — the per-version counts the
        # compare-versions history shows.
        request = CountTableRowsRequest(id=table_id, predicate=filter, version=version)
        with translate_catalog_errors():
            return int(self._api.count_table_rows(id_str, request, delimiter=self._delimiter, _request_timeout=self._timeout, _headers=self.request_headers()))

    def table_version(self, table_id: list[str]) -> int:
        # THEIR version primitive: a plain describe returns location-only (version
        # absent); load_detailed_metadata=true loads the dataset server-side and
        # returns the current version. The 409 handshake compares a client's
        # base_version against exactly this number. It asks for no credential: a vend
        # opens the table again and mints a store session, for a number this read never
        # reaches the store with ([[LH-529]]).
        from lance_namespace_urllib3_client import DescribeTableRequest
        from lance_namespace_urllib3_client.api.table_api import TableApi

        api = TableApi(self._api.api_client)
        with translate_catalog_errors():
            response = api.describe_table(
                self._delimiter.join(table_id),
                DescribeTableRequest(id=table_id, vend_credentials=False),
                delimiter=self._delimiter,
                load_detailed_metadata=True,
                _request_timeout=self._timeout,
                _headers=self.request_headers(),
            )
        if response.version is None:
            raise CatalogAnsweredError(f"catalog describe returned no version for {table_id}")
        return int(response.version)

    def list_versions(self, table_id: list[str], *, limit: int | None = None) -> list[CatalogVersion]:
        # THEIR listing primitive: ``POST /v1/table/{id}/version/list`` (reader-tier
        # ``can_get_metadata`` under the catalog's router authorize, audited app-wide).
        # ``descending=true`` is the spec-0.9 latest-to-oldest guarantee, so ``limit``
        # caps to the MOST RECENT versions server-side — no client-side re-sort.
        from lance_namespace_urllib3_client.api.table_api import TableApi

        api = TableApi(self._api.api_client)
        with translate_catalog_errors():
            response = api.list_table_versions(
                self._delimiter.join(table_id),
                delimiter=self._delimiter,
                limit=limit,
                descending=True,
                _request_timeout=self._timeout,
                _headers=self.request_headers(),
            )
        return [CatalogVersion(version=int(v.version), timestamp_millis=v.timestamp_millis) for v in response.versions or []]
