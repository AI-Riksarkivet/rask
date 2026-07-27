"""The shared read-back-and-emit trailer for catalog mutations that record versioned lineage.

Every endpoint whose native op commits a table change ends the same way: read the produced version +
per-version schema off the dataset, then publish a best-effort ``WROTE`` event. One helper (instead of a
copy per endpoint module) so the two correctness properties live in exactly one place:

* **single pinned open** — version and schema come from ONE ``open_dataset`` call, pinned to the version
  the write's response reported when it carries one, so a concurrent writer can never get version N+1's
  schema attached to version N's WROTE edge;
* **best-effort throughout** — the mutation is already committed when this runs, so a read-back failure
  degrades the lineage enrichment (versionless / schemaless emit) but never fails the request.

Ops that emit a versionless marker with no read-back (drop/deregister/declare/register) call
``emit_write_event`` directly.
"""

from __future__ import annotations

from typing import Any

from fastapi.concurrency import run_in_threadpool
from lance_namespace import LanceNamespace

from catalog.core.config import Settings
from catalog.core.lineage_emit import InputPin, LineageEmitter, emit_write_event
from catalog.services import dataplane
from catalog.services.dataplane import StorageOptions
from service_kit.governed.oidc import IDToken


async def emit_measured_write(
    emitter: LineageEmitter,
    segments: list[str],
    *,
    ns: LanceNamespace,
    so: StorageOptions,
    settings: Settings,
    token: IDToken | None,
    operation: str,
    authorization: str | None,
    pin_version: int | None = None,
    inputs: list[InputPin] | None = None,
    extra_run_facets: dict[str, Any] | None = None,
) -> None:
    """Read back ``(version, schema)`` in one pinned open, then emit the best-effort WROTE event.

    ``pin_version`` is the version the write's response reported (add/alter/drop columns, update, delete,
    merge_insert); ``None`` for ops whose response carries only a ``transaction_id`` (insert, index
    build/drop, restore, schema-metadata) — those read the current snapshot instead.

    ``inputs`` names the version-pinned source dataset(s) this write DERIVED FROM (a mover's merge from
    ``source@N``); ``extra_run_facets`` rides caller-supplied run facets (e.g. training ``params``) —
    both threaded verbatim to :func:`emit_write_event`, so the catalog stays un-opinionated about them.
    """
    version, schema_fields = await run_in_threadpool(dataplane.read_version_and_schema, ns, so, segments, pin_version)
    await emit_write_event(
        emitter,
        segments,
        delimiter=settings.delimiter,
        author=token.sub if token is not None else None,
        version=version,
        operation=operation,
        authorization=authorization,
        schema_fields=schema_fields,
        inputs=inputs,
        extra_run_facets=extra_run_facets,
    )
