"""A client's table read path, selected by its settings: the table opened directly, or read through the catalog.

``MEDIA_READ_BACKEND=direct`` reads an opened Lance dataset (:class:`LanceTableReader`); ``=catalog`` reads through the
catalog's ``/query`` door (:class:`CatalogTableReader`) over the live REST catalog, through the stock Lance Namespace SDK
(:mod:`client_kit.lancekit.catalog`), when ``MEDIA_CATALOG_URI`` is set, else over :class:`LocalCatalogTransport`, the
catalog's own read behaviour in process. Every path answers the one :class:`TableReader` surface, so a caller's code does
not change with the setting.
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

import lance
import pyarrow as pa
from lance_namespace import QueryTableRequest

from client_kit.lancekit.absence import reads_as_absent
from client_kit.lancekit.catalog import CatalogVersion, NamespaceCatalogTransport, connect_catalog
from service_kit.arrow_ipc import decode_arrow_response  # noqa: TID251 - the client's /query reader, as the lakehouse's is
from service_kit.exceptions import NotFoundError


@runtime_checkable
class TableReader(Protocol):
    """The read surface every read path answers.

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
    """A direct read of an opened Lance dataset, the default path."""

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


class CatalogTransport(Protocol):
    """Executes the catalog reads :class:`CatalogTableReader` needs: ``/query`` (Arrow IPC **file** bytes), a count, the
    current version and the version listing. :class:`~client_kit.lancekit.catalog.NamespaceCatalogTransport` answers them
    through the stock SDK and :class:`LocalCatalogTransport` in process, so the one reader runs over either."""

    def query(self, request: QueryTableRequest) -> bytes: ...

    def count(self, table_id: list[str], filter: str | None, *, version: int | None = None) -> int: ...

    def table_version(self, table_id: list[str]) -> int: ...

    def list_versions(self, table_id: list[str], *, limit: int | None = None) -> list[CatalogVersion]: ...


#: The bytes one page of a catalog read asks for: a quarter of the catalog's default 16 MiB answer ceiling
#: (`LANCE_QUERY_MAX_RESPONSE_MB`), so a page sized from a measured row width stays under it with room for the catalog
#: estimating that width differently ([[LH-247]]).
_PAGE_BYTES: Final = 4 << 20
#: The first page's row count, before any row width is known.
_FIRST_PAGE_ROWS: Final = 64


class CatalogTableReader:
    """Reads a table through the catalog's ``/query`` door; ``table_id`` is the catalog identifier as a list.

    The door is vector-search-first: it requires a ``vector`` and ``k`` and has no plain-scan verb, so a scan is an EMPTY
    vector, a bounded ``k`` and the filter. It is read in PAGES of a bounded ``k`` plus an ``offset``, every page pinned to
    one version: the catalog refuses a ``k`` whose answer could exceed its ceiling, because it builds each answer whole in
    memory, so an unbounded read is many bounded answers. The first page is :data:`_FIRST_PAGE_ROWS` rows; each later page
    is :data:`_PAGE_BYTES` worth of rows at the width the previous page measured, and a page shorter than its ``k`` is the
    last. Each answer is validated in full (:func:`~service_kit.arrow_ipc.decode_arrow_response`).
    """

    def __init__(self, transport: CatalogTransport, table_id: list[str]) -> None:
        self._transport = transport
        self._id = table_id

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
        """Scan the table page by page. ``version`` pins a historical snapshot (a gone version is the catalog's 404, a
        ``NotFoundError``); an unpinned read pins the version current when it starts, so its pages cannot straddle a commit.
        """
        pinned = version if version is not None else self.table_version()
        start = offset or 0
        pages: list[pa.Table] = []
        read = 0
        page_rows = _FIRST_PAGE_ROWS
        while True:
            k = max(1, page_rows if limit is None else min(page_rows, limit - read))
            request = QueryTableRequest(
                id=self._id,
                vector={"single_vector": []},
                k=k,
                columns={"column_names": columns} if columns else None,
                filter=filter,
                offset=(start + read) or None,
                with_row_id=with_row_id or None,
                version=pinned,
            )
            # A response from another service, validated in full like a caller's body, because framing that parses says
            # nothing about the buffers it frames. Not held to a size: the query bounds an answer, and an honest projection
            # can be as dense as a hostile body.
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
        """The table's version history, latest first, capped at ``limit``."""
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
            if reads_as_absent(exc):
                raise NotFoundError(f"table version {version} not found") from exc
            raise

    def query(self, request: QueryTableRequest) -> bytes:
        columns = request.columns.column_names if request.columns else None
        table = self._at_version(request.version).to_table(
            columns=columns,
            filter=request.filter,
            limit=request.k,
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


class TablePathSettings(Protocol):
    """What the path-selecting seams read: which path a table's reads and writes take, and the catalog that path
    goes through.

    A Protocol, so this kit depends on no service's settings class: a caller passes its own settings, and they fit by
    shape. Read-only members, so a settings field typed with a ``str`` subclass (a ``StrEnum`` backend) fits too.
    """

    @property
    def read_backend(self) -> str: ...

    @property
    def write_backend(self) -> str: ...

    @property
    def rest_catalog_mode(self) -> bool: ...

    @property
    def catalog_uri(self) -> str | None: ...

    @property
    def catalog_delimiter(self) -> str: ...


def open_reader(
    *,
    dataset: lance.LanceDataset | None,
    table_id: list[str],
    settings: TablePathSettings,
    caller_token: str | None = None,
) -> TableReader:
    """Select the read path from settings — the single host-agnostic seam.

    ``MEDIA_READ_BACKEND=direct`` (default) ⇒ :class:`LanceTableReader`. ``=catalog`` ⇒ :class:`CatalogTableReader` over the live REST catalog
    when ``MEDIA_CATALOG_URI`` is set, else the in-process
    :class:`LocalCatalogTransport`. ``dataset`` is the already-opened Lance table
    (the caller keeps ``table_dataset``'s 404 semantics); it backs the direct + local
    paths and is unused by the REST path.

    ``caller_token`` is the END USER's bearer, forwarded so the catalog's answer is about the
    CALLER — and it is the ONLY token this seam will send. A service credential would make every
    reader a confused deputy, because the catalog checks a relation on the `table:` object and
    injects no row predicate: an ungranted user gets 200 instead of 403.

    There is no fallback: every caller of this seam is a request-scoped route, so a settings token here would serve
    only an ANONYMOUS request, re-issued to the catalog under the estate's credential. Absent means absent; the
    catalog decides what an unauthenticated read is worth.

    A settings token is right only for a process with no caller at all. It is wrong on a request path.
    """
    if settings.read_backend != "catalog":
        if dataset is None:
            raise ValueError("direct read path requires an opened dataset")
        return LanceTableReader(dataset)
    if settings.catalog_uri:
        transport: CatalogTransport = NamespaceCatalogTransport(connect_catalog(settings.catalog_uri, token=caller_token, delimiter=settings.catalog_delimiter))
    else:
        if dataset is None:
            raise ValueError("in-process catalog fallback requires an opened dataset")
        transport = LocalCatalogTransport(dataset)
    return CatalogTableReader(transport, list(table_id))


def open_catalog_reader(*, table_id: list[str], settings: TablePathSettings, caller_token: str | None = None) -> CatalogTableReader:
    """The catalog-native reader, for surfaces only the catalog exposes (version
    listing + time-travel) — the seam behind paths gated on
    ``settings.rest_catalog_mode``, where the LIVE catalog owns the version
    number-space. Refuses any other configuration so a mixed/local deployment can
    never read history from the wrong number-space.
    """
    uri = settings.catalog_uri
    if not settings.rest_catalog_mode or not uri:
        raise ValueError("catalog version surface requires full catalog mode (MEDIA_CATALOG_URI set + both MEDIA_*_BACKEND=catalog)")
    transport = NamespaceCatalogTransport(connect_catalog(uri, token=caller_token, delimiter=settings.catalog_delimiter))
    return CatalogTableReader(transport, list(table_id))


def _to_arrow_file(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()
