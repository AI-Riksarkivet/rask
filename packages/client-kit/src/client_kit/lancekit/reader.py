"""A client's table read path, selected by its settings: the table opened directly, or read through the catalog.

``MEDIA_READ_BACKEND=direct`` reads an opened Lance dataset (:class:`LanceTableReader`); ``=catalog`` reads through the
catalog's ``/query`` door (:class:`~service_kit.lancekit.reader.CatalogTableReader`) over the live REST catalog when
``MEDIA_CATALOG_URI`` is set, else over :class:`LocalCatalogTransport`, the catalog's own read behaviour in process.
Every path answers the one :class:`TableReader` surface, so a caller's code does not change with the setting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

import lance
import pyarrow as pa

from service_kit.exceptions import NotFoundError
from service_kit.lancekit.absence import reads_as_absent
from service_kit.lancekit.reader import CatalogTableReader, CatalogTransport, CatalogVersion, RestCatalogTransport


if TYPE_CHECKING:
    from lance_namespace_urllib3_client import QueryTableRequest


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
    transport = RestCatalogTransport(uri, delimiter=settings.catalog_delimiter, token=caller_token)
    return CatalogTableReader(transport, list(table_id))


def _to_arrow_file(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()
