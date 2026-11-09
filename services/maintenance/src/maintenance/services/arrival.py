"""The commit lane: which announced events put a task on the work queue (ADR 0198 §2).

A commit is what makes a table's maintenance due sooner than its clock: a write moves its fragment and index counts, and
a bring-your-own job's compaction is a write through the catalog like any other, so the pass after it takes the table
off the due list. The lane reads the one announcement every governed writer converges on, `lineage.events.v1`, which
also carries the catalog's lifecycle markers, and maps each event to at most one task (:mod:`maintenance.services.queue`):

* A **write** or a **registration** holds its table's pass a delay out (``MAINTENANCE_COMMIT_PASS_DELAY_SECONDS``), unless
  one is held already, which covers it (:meth:`maintenance.services.queue.TaskQueue.follow_up`): a burst of commits makes
  one pass, at most one per delay, and that pass reads the table after every commit it covers. A create is announced as a
  write, and so is a publication's tag move. A registration (``register_table``, which both undrop doors send too) waits
  the same delay, which an undrop needs: it announces the table before it clears the trash record, and a pass that read
  the record in between would end the table's schedule.
* A **drop** (``drop_table``) queues the trash task of the table's uuid (:mod:`maintenance.services.trash_task`), which
  holds itself until the record's release, or ends at once when the drop filed no record.
* A declaration, a deregistration and a rename move no bytes and keep the table's uuid and location, so they are
  acknowledged untouched, and so is maintenance's own run (``expire_versions``), which the pass that emitted it has already
  followed with the table's next schedule. A declared table's first pass comes with its first commit.

**The event is a hint; the pass is the decision.** A write event carries the table and its location, and no fragment or
row count, so nothing here can tell whether work is due, only that it is worth looking.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel

from lakehouse.lineage_events import LANCE_FACET, dataset_uuids


#: Operations that ask nothing of maintenance. A declaration holds no data yet; a deregistration and a rename move no
#: bytes and keep the table's uuid and location, which its task and its schedule are keyed by; ``expire_versions`` is
#: maintenance's own run, announced on the topic this lane reads.
_UNTOUCHED: Final = frozenset({"declare_table", "deregister_table", "rename_table", "expire_versions"})
_DROP: Final = "drop_table"


class LaneTask(BaseModel):
    """The task one event asks for: a table's pass, held a delay out, or a dropped table's release, queued now."""

    kind: Literal["table", "trash"]
    #: The table's uuid as the event names it; ``None`` when it names none, and the handler reads the claim at
    #: ``location`` for it.
    uuid: str | None = None
    #: The table's physical location from the event's ``dataSource`` facet; empty for a drop, which names none.
    location: str = ""


def lane_task(event: object) -> LaneTask | None:
    """The task this lane would queue for ``event``, or ``None`` when it acknowledges the event untouched.

    ONE DECISION FOR THE DOOR AND THE HANDLER ([[XC-078]]). `/maintenance-arrival` checks the signature of exactly the
    events this selects and the handler queues exactly those, so what the lane acts on cannot widen past what the door
    checks. FAILS CLOSED on anything it cannot read, a CloudEvent whose `data` is not an object included: events arrive
    off a bus and are client-controlled, so a malformed one is ignored rather than raised, which would turn one bad
    publish into a redelivery loop. A pass needs the table's location and a release needs its uuid, so a write naming no
    location and a drop naming no uuid are acknowledged without a read: a pass must never be pointed at a location nobody
    confirmed.
    """
    if not isinstance(event, dict):
        return None
    run = event.get("run")
    facets = run.get("facets") if isinstance(run, dict) else None
    lance = facets.get(LANCE_FACET) if isinstance(facets, dict) else None
    operation = lance.get("operation") if isinstance(lance, dict) else None
    if not isinstance(operation, str) or operation in _UNTOUCHED or (output := _first_output(event)) is None:
        return None
    uuid = dataset_uuids(output).table_uuid
    if operation == _DROP:
        return LaneTask(kind="trash", uuid=uuid) if uuid else None
    if (location := _location_of(output)) is None:
        return None
    return LaneTask(kind="table", uuid=uuid, location=location)


def _first_output(event: dict[str, Any]) -> dict[str, Any] | None:
    """The first output the event names a dataset by, or ``None``: a catalog event names the one table it is about."""
    outputs = event.get("outputs")
    if not isinstance(outputs, list):
        return None
    return next((output for output in outputs if isinstance(output, dict) and isinstance(output.get("name"), str) and output["name"]), None)


def _location_of(output: dict[str, Any]) -> str | None:
    """The output's physical URI from the standard ``dataSource`` facet, or ``None`` when absent: a producer outside the
    catalog may emit a write event without one, and its table is found by its next pass or the audit."""
    facets = output.get("facets")
    source = facets.get("dataSource") if isinstance(facets, dict) else None
    uri = source.get("uri") if isinstance(source, dict) else None
    return uri if isinstance(uri, str) and uri else None
