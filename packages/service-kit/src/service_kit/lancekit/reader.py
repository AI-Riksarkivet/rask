"""Read-path abstraction: direct Lance today, lance-ns catalog tomorrow.

The backend opens Lance tables directly — fast, S3-capable, blob-seekable. The
lance-ns merge target instead expects reads to go through the catalog's
``POST /v1/table/{id}/query`` endpoint so the lakehouse owns credentials + authz
(see docs/LANCE_NS_INTEGRATION.md). This module puts both behind ONE duck-typed
:class:`TableReader` so call sites don't change; a flag (``MEDIA_READ_BACKEND``)
selects which.

Designed host-agnostic + retry-friendly so the catalog path drops into a Dapr
service-invocation or a Ray-Serve-fronted catalog without touching callers
(project_merge_runtime_stack): the base URL comes from settings, never a
hard-coded host, and the transport carries a urllib3 ``Retry``.

Scope (prototype): the dominant read pattern — a scan
``to_table(columns=, filter=, limit=, offset=, with_row_id=)`` plus
``count_rows`` — is covered end to end, and so is the VERSION surface the
compare-versions panel needs: version listing (``POST /v1/table/{id}/version/list``,
reader-tier governed) and time-travel (the ``version`` pin on ``/query`` /
``count_rows``, both honored by the native backend). Blob serving (``take_blobs``
by stable ``_rowid``) and hybrid ``search`` are the riskier phases the analysis
flags and stay ``direct`` (they can remain direct per-call even when the flag is
``catalog``); wiring them is follow-up.

The catalog ``/query`` endpoint is vector-search-first: it requires a ``vector``
and ``k`` and has no plain-scan verb, so a scan is expressed as an EMPTY vector +
a large ``k`` + the filter. Whether a live REST server accepts an empty vector as
a scan is the open item to probe against a running catalog; the in-process
:class:`LocalCatalogTransport` (the catalog's native behavior) implements exactly
that, which is what the parity test exercises.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import lance
import pyarrow as pa
from pydantic import BaseModel

from service_kit.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    UnauthorizedError,
    ValidationError,
)
from service_kit.lakehouse.naming import CATALOG_DELIMITER


log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from lance_namespace_urllib3_client import QueryTableRequest

    from service_kit.media.config import Settings


@runtime_checkable
class TableReader(Protocol):
    """The read surface every backend read path uses — the seam we can swap.

    Deliberately the SUBSET of the ``lance.LanceDataset`` API our scan paths call,
    so :class:`LanceTableReader` is a zero-cost pass-through and the catalog reader
    is duck-type compatible.
    """

    def to_table(
        self,
        *,
        columns: list[str] | None = None,
        filter: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        with_row_id: bool = False,
    ) -> pa.Table: ...

    def count_rows(self, filter: str | None = None) -> int: ...

    def table_version(self) -> int: ...

    @property
    def schema(self) -> pa.Schema: ...


class LanceTableReader:
    """Direct Lance open — verbatim today's behavior (the byte-identical default)."""

    def __init__(self, ds: lance.LanceDataset) -> None:
        self._ds = ds

    def to_table(
        self,
        *,
        columns: list[str] | None = None,
        filter: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        with_row_id: bool = False,
    ) -> pa.Table:
        return self._ds.to_table(
            columns=columns,
            filter=filter,
            limit=limit,
            offset=offset,
            with_row_id=with_row_id,
        )

    def count_rows(self, filter: str | None = None) -> int:
        return self._ds.count_rows(filter)

    def table_version(self) -> int:
        return int(self._ds.version)

    @property
    def schema(self) -> pa.Schema:
        return self._ds.schema


class CatalogVersion(BaseModel):
    """One entry of the catalog's version listing (``/v1/table/{id}/version/list``):
    the Lance version number + its commit timestamp in epoch millis (``None`` when
    the backend records none)."""

    version: int
    timestamp_millis: int | None = None


class CatalogTransport(Protocol):
    """Executes the catalog reads :class:`CatalogTableReader` needs — ``/query``
    (Arrow IPC **file** bytes), ``/count_rows`` and ``/version/list``.

    Mirrors the calls the reader makes on the lance-ns ``DataApi``/``TableApi``;
    abstracted so the same reader runs against a live REST catalog
    (:class:`RestCatalogTransport`) or an in-process namespace
    (:class:`LocalCatalogTransport`) with no code change.
    """

    def query(self, request: QueryTableRequest) -> bytes: ...

    def count(self, table_id: list[str], filter: str | None, *, version: int | None = None) -> int: ...

    def table_version(self, table_id: list[str]) -> int: ...

    def list_versions(self, table_id: list[str], *, limit: int | None = None) -> list[CatalogVersion]: ...


class CatalogTableReader:
    """Reads a table through the lance-ns catalog ``/query`` contract.

    ``table_id`` is the catalog identifier as a list (``[namespace, table]``);
    ``scan_k`` is the large ``k`` used to express a filter/columns-only scan (the
    endpoint has no plain-scan verb). Decodes the ``application/vnd.apache.arrow.file``
    response with :func:`pyarrow.ipc.open_file`.
    """

    def __init__(
        self,
        transport: CatalogTransport,
        table_id: list[str],
        *,
        scan_k: int = 1_000_000_000,
    ) -> None:
        self._transport = transport
        self._id = table_id
        self._scan_k = scan_k

    def _build_request(
        self,
        *,
        columns: list[str] | None,
        filter: str | None,
        limit: int | None,
        offset: int | None,
        with_row_id: bool,
        version: int | None,
    ) -> QueryTableRequest:
        from lance_namespace_urllib3_client import (  # optional dep: catalog backend only
            QueryTableRequest,
            QueryTableRequestColumns,
            QueryTableRequestVector,
        )

        return QueryTableRequest(
            id=self._id,
            # empty vector + large k ⇒ scan intent (no plain-scan verb exists)
            vector=QueryTableRequestVector(single_vector=[]),
            k=limit if limit is not None else self._scan_k,
            columns=QueryTableRequestColumns(column_names=columns) if columns else None,
            filter=filter,
            offset=offset,
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
        """Scan the table — ``version`` pins a HISTORICAL snapshot (catalog time-travel;
        a bad/reclaimed version is the catalog's 404, translated to ``NotFoundError``).
        The extra defaulted kwarg keeps this a :class:`TableReader`."""
        request = self._build_request(
            columns=columns,
            filter=filter,
            limit=limit,
            offset=offset,
            with_row_id=with_row_id,
            version=version,
        )
        raw = self._transport.query(request)
        return pa.ipc.open_file(pa.py_buffer(raw)).read_all()

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


class LocalCatalogTransport:
    """In-process catalog transport — the catalog's native read behavior over a
    local ``lance.LanceDataset``, no HTTP server.

    Faithful to what the native ``DirectoryNamespace`` does server-side: translate
    the :class:`QueryTableRequest` into a ``to_table`` scan (empty vector ⇒ scan)
    and serialize the result as an Arrow IPC **file** — the exact wire encoding the
    REST ``/query`` returns. Useful for offline/dev and as the reference the parity
    test checks the direct path against.
    """

    def __init__(self, ds: lance.LanceDataset) -> None:
        self._ds = ds

    def _at_version(self, version: int | None) -> lance.LanceDataset:
        """Time-travel to ``version`` (``None`` = current), translating Lance's raw
        not-found into ``NotFoundError`` — the native backend raises
        ``TableVersionNotFoundError`` (→ REST 404 → ``NotFoundError``), so the
        in-process transport must surface the same shape."""
        if version is None:
            return self._ds
        try:
            return self._ds.checkout_version(version)
        except (ValueError, FileNotFoundError) as exc:
            raise NotFoundError(f"table version {version} not found") from exc
        except OSError as exc:
            if "not found" in str(exc).lower():
                raise NotFoundError(f"table version {version} not found") from exc
            raise

    def query(self, request: QueryTableRequest) -> bytes:
        columns = request.columns.column_names if request.columns else None
        table = self._at_version(request.version).to_table(
            columns=columns,
            filter=request.filter,
            limit=request.k if request.k is not None and request.k < 1_000_000_000 else None,
            offset=request.offset,
            with_row_id=bool(request.with_row_id),
        )
        return _to_arrow_file(table)

    def count(self, table_id: list[str], filter: str | None, *, version: int | None = None) -> int:
        del table_id  # bound to a single ds; the id is implied
        return self._at_version(version).count_rows(filter)

    def table_version(self, table_id: list[str]) -> int:
        del table_id
        return int(self._ds.version)

    def list_versions(self, table_id: list[str], *, limit: int | None = None) -> list[CatalogVersion]:
        del table_id
        entries = [
            CatalogVersion(version=int(v["version"]), timestamp_millis=_epoch_millis(v.get("timestamp")))
            for v in reversed(self._ds.versions())  # descending, like the REST listing
        ]
        return entries[:limit] if limit is not None else entries


def _epoch_millis(ts: object) -> int | None:
    """A Lance ``versions()`` timestamp (a datetime) → epoch millis; ``None`` when
    absent or not datetime-shaped (the listing survives an odd manifest)."""
    to_epoch = getattr(ts, "timestamp", None)
    return int(to_epoch() * 1000) if callable(to_epoch) else None


@contextmanager
def translate_catalog_errors() -> Iterator[None]:
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
        # 401 is NOT 403, and collapsing them cost real debugging time: the annotator sent no
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
    service-invocation or Ray-Serve-fronted catalog unchanged. Exercised live
    against the lance-ns catalog over RustFS by ``tests/test_catalog_live.py``
    (empty-vector scan accepted; responses are Arrow-IPC files).
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
        from lance_namespace_urllib3_client import ApiClient, Configuration  # optional dep
        from lance_namespace_urllib3_client.api.data_api import DataApi

        config = Configuration(host=base_url, retries=retries)
        client = ApiClient(config)
        if token:
            client.set_default_header("Authorization", f"Bearer {token}")
        self._api = DataApi(client)
        self._delimiter = delimiter
        self._timeout = timeout

    def query(self, request: QueryTableRequest) -> bytes:
        id_str = self._delimiter.join(request.id or [])
        with translate_catalog_errors():
            raw = self._api.query_table(id_str, request, delimiter=self._delimiter, _request_timeout=self._timeout)
        return bytes(raw)

    def count(self, table_id: list[str], filter: str | None, *, version: int | None = None) -> int:
        from lance_namespace_urllib3_client import CountTableRowsRequest

        id_str = self._delimiter.join(table_id)
        # ``version`` pins the count at a historical snapshot (spec 0.9; the native
        # backend checks out that version server-side) — the per-version counts the
        # compare-versions history shows.
        request = CountTableRowsRequest(id=table_id, predicate=filter, version=version)
        with translate_catalog_errors():
            return int(self._api.count_table_rows(id_str, request, delimiter=self._delimiter, _request_timeout=self._timeout))

    def table_version(self, table_id: list[str]) -> int:
        # THEIR version primitive: a plain describe returns location-only (version
        # absent); load_detailed_metadata=true loads the dataset server-side and
        # returns the current version. The 409 handshake compares a client's
        # base_version against exactly this number.
        from lance_namespace_urllib3_client import DescribeTableRequest
        from lance_namespace_urllib3_client.api.table_api import TableApi

        api = TableApi(self._api.api_client)
        with translate_catalog_errors():
            response = api.describe_table(
                self._delimiter.join(table_id),
                DescribeTableRequest(id=table_id),
                delimiter=self._delimiter,
                load_detailed_metadata=True,
                _request_timeout=self._timeout,
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
            )
        return [CatalogVersion(version=int(v.version), timestamp_millis=v.timestamp_millis) for v in response.versions or []]


def open_reader(
    *,
    dataset: lance.LanceDataset | None,
    table_id: list[str],
    settings: Settings,
    caller_token: str | None = None,
) -> TableReader:
    """Select the read path from settings — the single host-agnostic seam.

    ``MEDIA_READ_BACKEND=direct`` (default) ⇒ :class:`LanceTableReader` (byte-identical
    to today). ``=catalog`` ⇒ :class:`CatalogTableReader` over the live REST catalog
    when ``MEDIA_CATALOG_URI`` is set, else the in-process
    :class:`LocalCatalogTransport`. ``dataset`` is the already-opened Lance table
    (the caller keeps ``table_dataset``'s 404 semantics); it backs the direct + local
    paths and is unused by the REST path.

    ``caller_token`` is the END USER's bearer, forwarded so the catalog's answer is about the
    CALLER — and it is the ONLY token this seam will send. A service credential would make every
    reader a confused deputy, because the catalog checks a relation on the `table:` object and
    injects no row predicate: an ungranted user gets 200 instead of 403.

    THERE IS NO FALLBACK, and this docstring used to promise one — "the settings token remains the
    fallback for callers with no request context (the publish saga, which outlives any request)".
    That saga does not exist in the tree. Every call site of this function is a request-scoped
    annotator route, so `caller_token or settings.catalog_token` fired only for the case it must not
    serve: an ANONYMOUS request, re-issued to the catalog under MEDIA_CATALOG_TOKEN. Absent now means
    absent; the catalog decides what an unauthenticated read is worth.

    The pattern is not wrong everywhere — the medallion's cascade movers genuinely have no caller and
    carry their own `MEDALLION_CATALOG_TOKEN`. It is wrong on a request path.
    """
    if settings.read_backend != "catalog":
        if dataset is None:
            raise ValueError("direct read path requires an opened dataset")
        return LanceTableReader(dataset)
    if settings.catalog_uri:
        transport: CatalogTransport = RestCatalogTransport(
            settings.catalog_uri,
            delimiter=settings.catalog_delimiter,
            token=caller_token,
        )
    else:
        if dataset is None:
            raise ValueError("in-process catalog fallback requires an opened dataset")
        transport = LocalCatalogTransport(dataset)
    return CatalogTableReader(transport, list(table_id))


def open_catalog_reader(*, table_id: list[str], settings: Settings, caller_token: str | None = None) -> CatalogTableReader:
    """The catalog-native reader, for surfaces only the catalog exposes (version
    listing + time-travel) — the seam behind paths gated on
    ``settings.rest_catalog_mode``, where the LIVE catalog owns the version
    number-space. Refuses any other configuration so a mixed/local deployment can
    never read history from the wrong number-space.
    """
    uri = settings.catalog_uri
    if not settings.rest_catalog_mode or not uri:
        raise ValueError("catalog version surface requires full catalog mode (MEDIA_CATALOG_URI set + both MEDIA_*_BACKEND=catalog)")
    transport = RestCatalogTransport(uri, delimiter=settings.catalog_delimiter, token=caller_token)
    return CatalogTableReader(transport, list(table_id))


def _to_arrow_file(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()
