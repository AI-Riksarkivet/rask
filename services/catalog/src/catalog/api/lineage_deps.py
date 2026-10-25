"""The shared read-back-and-emit trailer for catalog mutations that record versioned lineage.

Every endpoint whose native op commits a table change ends the same way: read the produced version +
per-version schema off the dataset, then publish a best-effort write event. One helper (instead of a
copy per endpoint module) so the two correctness properties live in exactly one place:

* **the commit, never a re-read** — the version is the one the write itself reported (a handle's
  version after the commit, a response's ``version``, or the version its ``transaction_id`` committed),
  and the schema comes from ONE open pinned to it, at the location the write committed at, so neither a
  concurrent writer nor a table created under the name since can lend its version or its schema to this
  event's output;
* **best-effort throughout** — the mutation is already committed when this runs, so a read-back failure
  degrades the lineage enrichment (versionless / schemaless emit) but never fails the request.

Ops that emit a versionless marker with no read-back (drop/deregister/declare/register) call
``emit_write_event`` directly.
"""

from __future__ import annotations

from fastapi.concurrency import run_in_threadpool

from catalog.api.security import Caller
from catalog.core import run_context
from catalog.core.lineage_emit import LineageEmitter, UuidReader, emit_write_event, run_inputs
from catalog.core.run_context import RunContext
from catalog.services import dataplane
from service_kit.lakehouse.objectfs import StorageOptions


def door_stamp(token: Caller | None, operation: str, run: RunContext | None = None) -> dict[str, str]:
    """The transaction properties a door stamps on the commit it makes ([[LH-497]]): the verified caller, the lineage
    ``operation`` it is announced as, and the run when the request names one (:func:`run_context.commit_properties`)."""
    return run_context.commit_properties(run, author=token.subject if token is not None else None, operation=operation)


async def emit_measured_write(
    emitter: LineageEmitter,
    segments: list[str],
    *,
    location: str,
    so: StorageOptions,
    token: Caller | None,
    operation: str,
    authorization: str | None,
    pin_version: int | None,
    branch: str | None,
    uuids: UuidReader,
    run: RunContext | None = None,
    commits_rows: bool = False,
) -> None:
    """Read back ``(version, schema)`` in one pinned open, then emit the best-effort write event.

    ``pin_version`` is the version the write COMMITTED. REQUIRED, with no default, so every door states
    which commit it made: a reopen cannot answer that, because under concurrent writes the latest snapshot
    is another writer's commit. A door whose response names no version resolves one with
    ``dataplane.committed_version`` from its ``transaction_id``; ``None`` means the commit could not be
    identified, and the event is versionless.

    ``branch`` is the ref the write COMMITTED TO (``None`` = main), required for the same reason: a branch
    has its own version sequence and its own schema, so a door that leaves it out records a branch write
    as a write to main. A branch write also carries that branch's ``branch_identifier``, because a recreated
    branch restarts its numbering and ``(ref, version)`` alone then names two commits.

    ``run`` is the run the writer named (:mod:`catalog.core.run_context`): its inputs become the event's
    version-pinned inputs, the derivation edge the catalog records for the write, and its id and job ride the
    event.

    ``commits_rows`` says the commit added rows, which makes a table's first version a write rather than a
    dataset change ([[LH-492]]).

    ``location`` is where the write committed: the location its door resolved ([[LH-530]]), or the one a create made.

    ``uuids`` reads the table's uuid from the claim at the location the read-back opened, the table the write landed in.
    """
    # A request may spell main by name; the event records main as no ref at all, as Lance does.
    ref = dataplane.recorded_branch(branch)
    readback = await run_in_threadpool(dataplane.read_version_and_schema, location, so, segments, pin_version, ref)
    identifier = await run_in_threadpool(dataplane.branch_identifier, location, so, segments, ref) if ref is not None else None
    await emit_write_event(
        emitter,
        segments,
        author=token.subject if token is not None else None,
        version=readback.version,
        operation=operation,
        authorization=authorization,
        uuids=uuids,
        schema_fields=readback.schema_fields,
        # The standard `dataSource` facet. Every write door reaches lineage through this trailer, so
        # omitting it here withheld the URI from ALL of them — the facet was plumbed end to end and
        # reachable from nothing, the same shape `test_originator_reaches_the_event.py` pins.
        source_uri=readback.uri,
        # THE REF FOLLOWS THE WRITE, not just the READ. The version was already read off the right ref
        # above — for the reason this function's own docstring gives — and then the ref was dropped, so
        # the event recorded a number that names two different snapshots (a branch and main keep
        # independent version sequences). Measured on the installed pylance: main v2, a branch write
        # gives branch v3, a later main write gives main v3, different contents.
        branch=ref,
        branch_identifier=identifier,
        inputs=run_inputs(run),
        run=run,
        commits_rows=commits_rows,
        # The commit's own time, so a re-announcement of this commit is the same event ([[LH-497]]).
        event_time=readback.committed_at,
    )
