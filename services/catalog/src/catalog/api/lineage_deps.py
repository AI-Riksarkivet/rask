"""The shared read-back-and-emit trailer for catalog mutations that record versioned lineage.

Every endpoint whose native op commits a table change ends the same way: read the produced version +
per-version schema off the dataset, then publish a best-effort ``WROTE`` event. One helper (instead of a
copy per endpoint module) so the two correctness properties live in exactly one place:

* **the commit, never a re-read** — the version is the one the write itself reported (a handle's
  version after the commit, a response's ``version``, or the version its ``transaction_id`` committed),
  and the schema comes from ONE open pinned to it, so a concurrent writer can never lend its version or
  its schema to this WROTE edge;
* **best-effort throughout** — the mutation is already committed when this runs, so a read-back failure
  degrades the lineage enrichment (versionless / schemaless emit) but never fails the request.

Ops that emit a versionless marker with no read-back (drop/deregister/declare/register) call
``emit_write_event`` directly.
"""

from __future__ import annotations

from typing import Any

from fastapi.concurrency import run_in_threadpool
from lance_namespace import LanceNamespace

from catalog.api.security import Principal
from catalog.core.config import Settings
from catalog.core.lineage_emit import InputPin, LineageEmitter, emit_write_event
from catalog.services import dataplane
from service_kit.lakehouse.objectfs import StorageOptions


async def emit_measured_write(
    emitter: LineageEmitter,
    segments: list[str],
    *,
    ns: LanceNamespace,
    so: StorageOptions,
    settings: Settings,
    token: Principal | None,
    operation: str,
    authorization: str | None,
    pin_version: int | None,
    branch: str | None,
    inputs: list[InputPin] | None = None,
    extra_run_facets: dict[str, Any] | None = None,
) -> None:
    """Read back ``(version, schema)`` in one pinned open, then emit the best-effort WROTE event.

    ``pin_version`` is the version the write COMMITTED. REQUIRED, with no default, so every door states
    which commit it made: a reopen cannot answer that, because under concurrent writes the latest snapshot
    is another writer's commit. A door whose response names no version resolves one with
    ``dataplane.committed_version`` from its ``transaction_id``; ``None`` means the commit could not be
    identified, and the event is versionless.

    ``branch`` is the ref the write COMMITTED TO (``None`` = main), required for the same reason: a branch
    has its own version sequence and its own schema, so a door that leaves it out records a branch write
    as a write to main. A branch write also carries that branch's ``branch_identifier``, because a recreated
    branch restarts its numbering and ``(ref, version)`` alone then names two commits.

    ``inputs`` names the version-pinned source dataset(s) this write DERIVED FROM (a stage runner's merge from
    ``source@N``); ``extra_run_facets`` rides caller-supplied run facets (e.g. training ``params``) —
    both threaded verbatim to :func:`emit_write_event`, so the catalog stays un-opinionated about them.
    """
    # A request may spell main by name; the event records main as no ref at all, as Lance does.
    ref = dataplane.recorded_branch(branch)
    version, schema_fields, location = await run_in_threadpool(dataplane.read_version_and_schema, ns, so, segments, pin_version, ref)
    identifier = await run_in_threadpool(dataplane.branch_identifier, ns, so, segments, ref) if ref is not None else None
    await emit_write_event(
        emitter,
        segments,
        delimiter=settings.delimiter,
        author=token.sub if token is not None else None,
        version=version,
        operation=operation,
        authorization=authorization,
        schema_fields=schema_fields,
        # The standard `dataSource` facet. Every write door reaches lineage through this trailer, so
        # omitting it here withheld the URI from ALL of them — the facet was plumbed end to end and
        # reachable from nothing, the same shape `test_originator_reaches_the_event.py` pins.
        source_uri=location,
        # THE REF FOLLOWS THE WRITE, not just the READ. The version was already read off the right ref
        # above — for the reason this function's own docstring gives — and then the ref was dropped, so
        # the event recorded a number that names two different snapshots (a branch and main keep
        # independent version sequences). Measured on the installed pylance: main v2, a branch write
        # gives branch v3, a later main write gives main v3, different contents.
        branch=ref,
        branch_identifier=identifier,
        inputs=inputs,
        extra_run_facets=extra_run_facets,
    )
