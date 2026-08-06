"""Write-path abstraction: direct Lance today, lance-ns catalog tomorrow.

The WRITE mirror of :mod:`service_kit.lancekit.reader`. Annotation saves write Lance
directly (merge_insert + delete); the merge target routes writes through the
catalog's ``POST /v1/table/{id}/merge_insert`` (+ ``/delete``) so the lakehouse
governs them — OpenFGA gate + OpenLineage emission + quality gate + credential
vending, all for free. Putting both behind ONE duck-typed :class:`TableWriter`
makes the merge a config flip (``MEDIA_WRITE_BACKEND``), not a rewrite.

Host-agnostic + retry-friendly by construction (base URL from settings, urllib3
``Retry``) so the catalog path drops into a Dapr service-invocation unchanged.

Scope: the ops ``annotate.py``'s writes perform — a merge UPSERT (update matched +
insert unmatched, keyed by ``id``), an INSERT-ONLY merge (insert unmatched, leave
matched untouched — used for tags so a re-save never clobbers a human's review), and
a delete-by-predicate.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import lance

from service_kit.exceptions import ConflictError
from service_kit.lancekit.reader import translate_catalog_errors


if TYPE_CHECKING:
    from collections.abc import Iterator

    import pyarrow as pa

    from service_kit.media.config import Settings


@runtime_checkable
class TableWriter(Protocol):
    """The write surface — the subset annotate.py Save uses. :class:`LanceTableWriter`
    is a pass-through; the catalog writer is duck-type compatible. The caller re-opens
    the table for the new version (mirroring today's code)."""

    def merge_upsert(self, delta: pa.Table, on: str) -> None: ...
    def merge_insert_only(self, delta: pa.Table, on: str) -> None: ...
    def delete(self, predicate: str) -> None: ...


#: A LOST COMMIT RACE, by message — pylance 9.0.0 exposes no typed error for it (probed: neither
#: ``lance.error`` nor the native module carries one), so the message is the only signal there is.
#: Same markers the catalog's own classifier uses (``catalog/services/dataplane.py``), kept identical
#: on purpose: one vocabulary for one condition, whichever plane hits it.
_COMMIT_CONFLICT_MARKERS = ("commit conflict", "concurrent")


@contextmanager
def translate_commit_conflict() -> Iterator[None]:
    """Turn a LOST COMMIT RACE on the direct path into the 409 the client already handles.

    The version check and the write are two separate steps (the endpoint reads
    ``reader.table_version()``, builds a delta, then commits), so a writer that lands in
    between passes the check and loses the commit. That is the ordinary OCC outcome and the
    caller's remedy is the same as for a stale ``base_version`` — re-read, rebuild, re-send.

    Untranslated it escaped as a raw ``OSError``, which no handler maps (only ``DomainError``
    and ``RequestValidationError`` are registered), so the browser got a 500. A 500 reads as
    "the server is broken" and is not retried; the annotator's client only branches on 409
    (``annotations-client.ts``). So the one moment the guarantee actually fires was reported
    as an outage, and the work was lost rather than merged.

    NOT retried here on purpose: the delta was built against the version that just lost, so
    re-committing it would overwrite the winner — precisely what OCC exists to stop. Only the
    caller can rebuild against the new version.
    """
    try:
        yield
    except OSError as exc:
        if any(m in str(exc).lower() for m in _COMMIT_CONFLICT_MARKERS):
            raise ConflictError(f"annotations changed on the server while this save was committing — re-read and re-send: {exc}") from exc
        raise


class LanceTableWriter:
    """Direct Lance writes — verbatim today's behavior (the byte-identical default)."""

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
        self._ds.merge_insert(on).when_matched_update_all().when_not_matched_insert_all().execute(delta)

    def merge_insert_only(self, delta: pa.Table, on: str) -> None:
        self._ds.merge_insert(on).when_not_matched_insert_all().execute(delta)

    def delete(self, predicate: str) -> None:
        self._ds.delete(predicate)


def _arrow_stream_bytes(delta: pa.Table) -> bytes:
    """Serialize the merge delta as an Arrow-IPC **stream** — the encoding the
    catalog's write endpoints parse (an IPC *file* body fails their Rust reader
    with "failed to fill whole buffer"). The read side returns IPC files — the
    asymmetry is the catalog's contract, not ours."""
    import pyarrow as pa_

    sink = pa_.BufferOutputStream()
    with pa_.ipc.new_stream(sink, delta.schema) as writer:
        writer.write_table(delta)
    return bytes(sink.getvalue())


class RestCatalogWriteTransport:
    """Live REST catalog transport — ``POST /v1/table/{id}/merge_insert`` + ``/delete``
    via the lance-ns client's ``DataApi.merge_insert_into_table`` / ``delete_from_table``.
    Host-agnostic (base URL from settings) + retry-friendly; catalog errors are
    translated onto the DomainError hierarchy so denials/conflicts keep their
    status through our services."""

    def __init__(
        self,
        base_url: str,
        table_id: list[str],
        *,
        delimiter: str = "$",
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
        self._id_str = delimiter.join(table_id)
        self._delimiter = delimiter
        self._timeout = timeout

    def merge_upsert(self, delta: pa.Table, on: str) -> None:
        # merge flags are direct params; `body` is the Arrow-IPC STREAM delta.
        with translate_catalog_errors():
            self._api.merge_insert_into_table(
                self._id_str,
                on,
                _arrow_stream_bytes(delta),
                delimiter=self._delimiter,
                when_matched_update_all=True,
                when_not_matched_insert_all=True,
                _request_timeout=self._timeout,
            )

    def merge_insert_only(self, delta: pa.Table, on: str) -> None:
        with translate_catalog_errors():
            self._api.merge_insert_into_table(
                self._id_str,
                on,
                _arrow_stream_bytes(delta),
                delimiter=self._delimiter,
                when_matched_update_all=False,
                when_not_matched_insert_all=True,
                _request_timeout=self._timeout,
            )

    def delete(self, predicate: str) -> None:
        from lance_namespace_urllib3_client import DeleteFromTableRequest

        with translate_catalog_errors():
            self._api.delete_from_table(
                self._id_str,
                DeleteFromTableRequest(predicate=predicate),
                delimiter=self._delimiter,
                _request_timeout=self._timeout,
            )


def open_writer(
    *,
    dataset: lance.LanceDataset | None,
    table_id: list[str],
    settings: Settings,
    caller_token: str | None = None,
) -> TableWriter:
    """Select the write path from settings — the single host-agnostic seam.

    ``MEDIA_WRITE_BACKEND=direct`` (default) ⇒ :class:`LanceTableWriter` (byte-identical
    to today). ``=catalog`` ⇒ :class:`CatalogTableWriter` over the live REST catalog
    when ``MEDIA_CATALOG_URI`` is set, else the in-process :class:`LocalCatalogWriteTransport`.

    ``caller_token`` is the END USER's bearer, forwarded so the catalog authorises the WRITE against
    the caller rather than against a service account — the same confused-deputy argument as
    :func:`open_reader`, and it bites harder on a write. Falls back to ``settings.catalog_token``
    for callers with no request context (the publish saga, which outlives any request).
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
            token=caller_token or settings.catalog_token,
        )
    else:
        if dataset is None:
            raise ValueError("in-process catalog fallback requires an opened dataset")
        transport = LocalCatalogWriteTransport(dataset)
    return CatalogTableWriter(transport, list(table_id))
