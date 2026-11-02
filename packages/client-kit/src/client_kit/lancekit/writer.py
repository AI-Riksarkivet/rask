"""A client's table write path, selected by its settings: the table written directly, or through the catalog.

``MEDIA_WRITE_BACKEND=direct`` writes an opened Lance dataset (:class:`LanceTableWriter`); ``=catalog`` writes through
the catalog's ``merge_insert`` and ``delete`` doors (:class:`~service_kit.lancekit.writer.RestCatalogWriteTransport`)
when ``MEDIA_CATALOG_URI`` is set, else through :class:`LocalCatalogWriteTransport`, the catalog's own write behaviour
in process. Every path answers the one :class:`TableWriter` surface: a merge upsert keyed on ``on``, an insert-only
merge that leaves a matched row as it is, and a delete by predicate.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import lance

from service_kit.exceptions import ConflictError, ValidationError
from service_kit.lancekit.commit_verdict import CommitVerdict, classify_commit_failure
from service_kit.lancekit.writer import RestCatalogWriteTransport


if TYPE_CHECKING:
    import pyarrow as pa

    from client_kit.lancekit.reader import TablePathSettings


@runtime_checkable
class TableWriter(Protocol):
    """The write surface — the three ops in the module scope. :class:`LanceTableWriter`
    is a pass-through; the catalog writer is duck-type compatible. The caller re-opens
    the table for the new version."""

    def merge_upsert(self, delta: pa.Table, on: str) -> None: ...
    def merge_insert_only(self, delta: pa.Table, on: str) -> None: ...
    def delete(self, predicate: str) -> None: ...


@contextmanager
def translate_commit_conflict() -> Generator[None]:
    """Turn a LOST COMMIT RACE on the direct path into the 409 the client already handles.

    The version check and the write are two separate steps (the endpoint reads
    ``reader.table_version()``, builds a delta, then commits), so a writer that lands in
    between passes the check and loses the commit. That is the ordinary OCC outcome and the
    caller's remedy is the same as for a stale ``base_version`` — re-read, rebuild, re-send.

    Untranslated it escaped as a raw ``OSError``, which no handler maps (only ``DomainError``
    and ``RequestValidationError`` are registered), so the browser got a 500. A 500 reads as
    "the server is broken" and is not retried; a client of a write door branches on 409.
    So the one moment the guarantee actually fires was reported
    as an outage, and the work was lost rather than merged.

    NOT retried here on purpose: the delta was built against the version that just lost, so
    re-committing it would overwrite the winner — precisely what OCC exists to stop. Only the
    caller can rebuild against the new version.
    """
    try:
        yield
    except OSError as exc:
        verdict = classify_commit_failure(exc)
        if verdict is CommitVerdict.RETRYABLE_CONFLICT:
            raise ConflictError(f"annotations changed on the server while this save was committing — re-read and re-send: {exc}") from exc
        if verdict is CommitVerdict.INCOMPATIBLE:
            # THE OTHER CONFLICT, and the reason this branch exists at all. Both messages mention
            # concurrency and only one is safe to retry: here the table was REPLACED underneath the
            # writer, so the delta describes rows that no longer belong and re-sending it is what
            # corrupts the table. Matching on the bare word `concurrent` answered this 409 "re-read
            # and re-send" — the one instruction the caller must not follow.
            raise ValidationError(
                "the table changed underneath this save — a concurrent overwrite replaced its contents. "
                f"This is NOT retryable: re-read the current version and rebuild from it; do not re-send: {exc}"
            ) from exc
        raise


class LanceTableWriter:
    """Direct writes to an opened Lance dataset, the default path."""

    def __init__(self, ds: lance.LanceDataset) -> None:
        self._ds = ds

    def merge_upsert(self, delta: pa.Table, on: str) -> None:
        with translate_commit_conflict():
            self._ds.merge_insert(on).when_matched_update_all().when_not_matched_insert_all().execute(delta)

    def merge_insert_only(self, delta: pa.Table, on: str) -> None:
        # Insert unmatched rows only — matched rows (same key) are left AS-IS, so a
        # re-save preserves any human review already made to an existing row.
        with translate_commit_conflict():
            self._ds.merge_insert(on).when_not_matched_insert_all().execute(delta)

    def delete(self, predicate: str) -> None:
        with translate_commit_conflict():
            self._ds.delete(predicate)


class CatalogWriteTransport(Protocol):
    """Executes a catalog write. Abstracted so the same writer runs against a live
    REST catalog (:class:`RestCatalogWriteTransport`) or an in-process namespace
    (:class:`LocalCatalogWriteTransport`) unchanged."""

    def merge_upsert(self, delta: pa.Table, on: str) -> None: ...
    def merge_insert_only(self, delta: pa.Table, on: str) -> None: ...
    def delete(self, predicate: str) -> None: ...


class CatalogTableWriter:
    """Writes a table through the lance-ns catalog. ``table_id`` is the catalog
    identifier (``[namespace, table]``)."""

    def __init__(self, transport: CatalogWriteTransport, table_id: list[str]) -> None:
        self._transport = transport
        self._id = table_id

    def merge_upsert(self, delta: pa.Table, on: str) -> None:
        self._transport.merge_upsert(delta, on)

    def merge_insert_only(self, delta: pa.Table, on: str) -> None:
        self._transport.merge_insert_only(delta, on)

    def delete(self, predicate: str) -> None:
        self._transport.delete(predicate)


class LocalCatalogWriteTransport:
    """In-process catalog transport — the catalog's native write behavior over a
    local ``lance.LanceDataset`` (no HTTP server). Faithful to what the native
    ``DirectoryNamespace`` does server-side, and what the parity test checks the
    direct path against."""

    def __init__(self, ds: lance.LanceDataset) -> None:
        self._ds = ds

    def merge_upsert(self, delta: pa.Table, on: str) -> None:
        with translate_commit_conflict():
            self._ds.merge_insert(on).when_matched_update_all().when_not_matched_insert_all().execute(delta)

    def merge_insert_only(self, delta: pa.Table, on: str) -> None:
        with translate_commit_conflict():
            self._ds.merge_insert(on).when_not_matched_insert_all().execute(delta)

    def delete(self, predicate: str) -> None:
        with translate_commit_conflict():
            self._ds.delete(predicate)


def open_writer(
    *,
    dataset: lance.LanceDataset | None,
    table_id: list[str],
    settings: TablePathSettings,
    caller_token: str | None = None,
) -> TableWriter:
    """Select the write path from settings — the single host-agnostic seam.

    ``MEDIA_WRITE_BACKEND=direct`` (default) ⇒ :class:`LanceTableWriter`. ``=catalog`` ⇒ :class:`CatalogTableWriter` over the live REST catalog
    when ``MEDIA_CATALOG_URI`` is set, else the in-process :class:`LocalCatalogWriteTransport`.

    ``caller_token`` is the END USER's bearer, forwarded so the catalog authorises the WRITE against
    the caller rather than against a service account — the same confused-deputy argument as
    :func:`open_reader`, and it bites harder on a write.

    No fallback: an absent caller token sends no token, so an anonymous request is never re-issued under the
    estate's own credential, on a write least of all.
    """
    if settings.write_backend != "catalog":
        if dataset is None:
            raise ValueError("direct write path requires an opened dataset")
        return LanceTableWriter(dataset)
    if settings.catalog_uri:
        transport: CatalogWriteTransport = RestCatalogWriteTransport(
            settings.catalog_uri,
            list(table_id),
            delimiter=settings.catalog_delimiter,
            token=caller_token,
        )
    else:
        if dataset is None:
            raise ValueError("in-process catalog fallback requires an opened dataset")
        transport = LocalCatalogWriteTransport(dataset)
    return CatalogTableWriter(transport, list(table_id))
