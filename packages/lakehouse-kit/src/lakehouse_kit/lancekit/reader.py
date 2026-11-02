"""The catalog's read transport: a table read through the spec's REST doors.

:class:`CatalogTableReader` reads a table through ``POST /v1/table/{id}/query`` (a scan, paged and pinned to one
version), ``/count_rows``, ``describe`` (the current version) and ``/version/list``, over a :class:`CatalogTransport`;
:class:`RestCatalogTransport` is that transport over HTTP. The base URL comes from the caller, never a hard-coded host,
and the transport carries a urllib3 ``Retry``, so it runs behind a Dapr service invocation or a proxy unchanged.

The ``/query`` door is vector-search-first: it requires a ``vector`` and ``k`` and has no plain-scan verb, so a scan
is an EMPTY vector, a bounded ``k`` and the filter.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, Protocol

import pyarrow as pa
from pydantic import BaseModel

from lakehouse_kit.lancekit.catalog_client import catalog_api_client
from lakehouse_kit.lancekit.catalog_client import request_headers as _request_headers
from service_kit.arrow_ipc import decode_arrow_response
from service_kit.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    UnauthorizedError,
    ValidationError,
)
from service_kit.naming import CATALOG_DELIMITER


log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from lance_namespace_urllib3_client import QueryTableRequest


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
    many bounded answers, never one. The first page is :data:`_FIRST_PAGE_ROWS` rows; each later page is
    :data:`_PAGE_BYTES` worth of rows at the width the previous page measured. A page shorter than the
    ``k`` it asked for is the last. Decodes each ``application/vnd.apache.arrow.file`` response with
    :func:`~service_kit.arrow_ipc.decode_arrow_response`, which validates it in full and raises
    :class:`~service_kit.arrow_ipc.ArrowBodyError` for bytes that are not a valid Arrow IPC body.
    """

    def __init__(self, transport: CatalogTransport, table_id: list[str]) -> None:
        self._transport = transport
        self._id = table_id

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
        a bad/reclaimed version is the catalog's 404, translated to ``NotFoundError``), and an unpinned
        read pins the version current when it starts, so its pages cannot straddle a commit.
        """
        pinned = version if version is not None else self.table_version()
        start = offset or 0
        pages: list[pa.Table] = []
        read = 0
        page_rows = _FIRST_PAGE_ROWS
        while True:
            k = max(1, page_rows if limit is None else min(page_rows, limit - read))
            request = self._build_request(columns=columns, filter=filter, k=k, offset=start + read, with_row_id=with_row_id, version=pinned)
            # The response is bytes from another service, validated in full like a caller's body, because
            # framing that parses says nothing about the buffers it frames. It is not held to its size as a
            # body is: the query bounds a response, and an honest projection can be as dense as a hostile body.
            page = decode_arrow_response(self._transport.query(request))
            pages.append(page)
            read += page.num_rows
            if page.num_rows < k or (limit is not None and read >= limit):
                break
            page_rows = max(1, _PAGE_BYTES // -(-page.nbytes // page.num_rows))
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


@contextmanager
def translate_catalog_errors() -> Generator[None]:
    """Map the generated client's typed HTTP exceptions onto our DomainError
    hierarchy, so a catalog 409/403/404/400/5xx renders as the SAME problem+json
    the direct path produces instead of an opaque 500. The catalog's own problem
    detail rides in the message."""
    from lance_namespace_urllib3_client import exceptions as _api_exc  # optional dep

    try:
        yield
    except _api_exc.ConflictException as exc:
        raise ConflictError(f"catalog write conflict: {exc.body}") from exc
    except _api_exc.UnauthorizedException as exc:
        # 401 is NOT 403, and collapsing them cost real debugging time: a client sent no
        # bearer, the catalog answered 401, and the browser was told 403 — which reads as "you lack
        # a grant" and sends you looking for a missing FGA tuple that was never the problem. A
        # missing/invalid credential must stay 401 so the next reader can tell authn from authz.
        raise UnauthorizedError(f"catalog rejected the credential: {exc.body}") from exc
    except _api_exc.ForbiddenException as exc:
        raise ForbiddenError(f"catalog denied the request: {exc.body}") from exc
    except _api_exc.NotFoundException as exc:
        raise NotFoundError(f"catalog table not found: {exc.body}") from exc
    except (_api_exc.BadRequestException, _api_exc.UnprocessableEntityException) as exc:
        raise ValidationError(f"catalog rejected the request: {exc.body}") from exc
    except _api_exc.ApiException as exc:
        # A STABLE SENTENCE, not the upstream body — and this branch specifically, not the four above.
        #
        # `ns_errors.problem_detail` replaces `str(exc)` with a fixed "Internal Server Error" on every
        # 5xx so paths, DSNs and driver text cannot reach a client; `_UNREDACTED_5XX` has exactly one
        # member and says why. Relaying `exc.body` here routed around that: a 503 whose detail is
        # whatever answered, rendered verbatim because `service_kit`'s handler keeps 4xx-style messages.
        #
        # The 4xx branches above are DIFFERENT and stay as they are: there the body IS the catalog's own
        # redacted problem+json, and relaying it is what turns an opaque 500 into the answer the direct
        # path gives — their rationale is recorded and correct. This branch is the one that fires when
        # the responder is NOT the catalog, and on the failure it actually catches that is usually an
        # ingress or sidecar returning an HTML error page or a proxy diagnostic.
        log.exception("catalog_unavailable", extra={"reason": exc.reason, "body": exc.body})
        raise ServiceUnavailableError("the catalog is unavailable") from exc


class RestCatalogTransport:
    """Live REST catalog transport — ``POST /v1/table/{id}/query`` over HTTP.

    Host-agnostic (base URL from settings, never hard-coded) + retry-friendly
    (urllib3 ``Retry`` via the generated ``Configuration``) so it drops into a Dapr
    service-invocation or proxy-fronted catalog unchanged. Exercised against the catalog app over HTTP by
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
        self._api = DataApi(catalog_api_client(base_url, retries=retries))
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
            raise ServiceUnavailableError(f"catalog describe returned no version for {table_id}")
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
