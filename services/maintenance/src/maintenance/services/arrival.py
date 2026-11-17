"""The commit lane: which announced events put a task on the work queue, and the worker's handling of each (ADR 0198 §2).

A commit is what makes a table's maintenance due sooner than its clock: a write moves its fragment and index counts, and
a bring-your-own job's compaction is a write through the catalog like any other, so the pass after it takes the table
off the due list. The lane reads the one announcement every governed writer converges on, `lineage.events.v1`, which
also carries the catalog's lifecycle markers, and maps each event to at most one task (:mod:`maintenance.services.queue`):

* A **write** or a **registration** holds its table's pass a delay out (``MAINTENANCE_COMMIT_PASS_DELAY_SECONDS``), unless
  one is held already, which covers it (:meth:`maintenance.services.queue.TaskQueue.follow_up`): a burst of commits makes
  one pass, at most one per delay, and that pass reads the table after every commit it covers. A create is announced as a
  write. A registration (``register_table``, which both undrop doors send too) waits the same delay, which an undrop
  needs: it announces the table before it clears the trash record, and a pass that read the record in between would end
  the table's schedule. So a registration holds a follow-up of its own, replacing any held, never covered by it: a
  follow-up a write held before the drop fires on its own older time, which may fall between the undrop's announcement
  and its clear (ADR 0232, assumption A6). Replaced, that older pass comes up to a delay later, and reads every commit
  it covered.
* A **drop** (``drop_table``) queues the trash task of the table's uuid (:mod:`maintenance.services.trash_task`), which
  holds itself until the record's release, or ends at once when the drop filed no record.
* A declaration, a deregistration and a rename move no bytes and keep the table's uuid and location, so they are
  acknowledged untouched, and so is maintenance's own run (``expire_versions``), which the pass that emitted it has already
  followed with the table's next schedule. A declared table's first pass comes with its first commit.

**The event is a hint; the pass is the decision.** A write event carries the table and its location, and no fragment or
row count, so nothing here can tell whether work is due, only that it is worth looking.

**The worker pulls the lane itself** (:class:`Arrivals`, :meth:`maintenance.services.queue.Worker.run_arrivals`), from a
durable consumer of `lineage.events.v1` the chart's stream Job declares, so its replicas compete for each announcement
and one of them holds the task it asks for.

A WRITE IS ITS PUBLISHER'S CLAIM UNTIL ITS SIGNATURE IS CHECKED ([[XC-078]], owner ruling R4). The bus's permission
table says who may publish on the topic, not who wrote a given event, and the location a write names is what this
service reads. So before it acts, the lane checks the write's `rask_signature` against the keys its signer publishes,
with the signer sets lineage verifies with (`RASK_EVENT_SIGNERS`, `RASK_EVENT_DELEGATORS`), and `RASK_SIGNATURE_DOORS`
decides what it does with the verdict (`lakehouse.lineage.door`): off checks nothing, observe acts as before and counts
what enforcing would refuse, and enforce acts only on a write a listed signer's key verifies. A refusal is acknowledged
and holds nothing, since no redelivery can sign bytes already published; a key list that cannot be read is asked for
again, since it says nothing about the signature. Only an event the lane would hold a task for is checked
(:func:`lane_task`), so an event it acknowledges untouched costs no key read.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from lakehouse import location_claims
from lakehouse.bus import processing
from lakehouse.bus_delivery import envelope_of
from lakehouse.governed.signing_key import published_key_fetch
from lakehouse.governed.store_credential import StoreCredentialUnavailableError
from lakehouse.lineage.door import DoorMode, DoorVerdict, judge
from lakehouse.lineage.keys import PublishedKeys
from lakehouse.lineage.signing import signature_of, verify_signature
from lakehouse.lineage_events import LANCE_FACET, dataset_uuids
from maintenance.core.metrics import ArrivalOutcome, SignatureDoor, record_signature_refused, record_signature_would_refuse
from maintenance.services.naming import claim_store
from maintenance.services.queue import ARRIVALS_CONSUMER, Task, TaskQueue


if TYPE_CHECKING:
    from maintenance.core.config import MaintenanceSettings


log = logging.getLogger(__name__)

#: This lane's name on the signature counters and log lines.
ARRIVAL_DOOR: Final[SignatureDoor] = "maintenance-arrival"
#: Bounds what an event's own text can put into a log line: the identity and the ids it carries are its publisher's.
_MAX_LOGGED_CHARS: Final = 300


#: Operations that ask nothing of maintenance. A declaration holds no data yet; a deregistration and a rename move no
#: bytes and keep the table's uuid and location, which its task and its schedule are keyed by; ``expire_versions`` is
#: maintenance's own run, announced on the topic this lane reads.
_UNTOUCHED: Final = frozenset({"declare_table", "deregister_table", "rename_table", "expire_versions"})
_DROP: Final = "drop_table"
#: The registration, which both undrop doors announce too: its pass replaces the one held.
_REGISTER: Final = "register_table"


class LaneTask(BaseModel):
    """The task one event asks for: a table's pass, held a delay out, or a dropped table's release, queued now."""

    kind: Literal["table", "trash"]
    #: The table's uuid as the event names it; ``None`` when it names none, and the handler reads the claim at
    #: ``location`` for it.
    uuid: str | None = None
    #: The table's physical location from the event's ``dataSource`` facet; empty for a drop, which names none.
    location: str = ""
    #: Whether the pass replaces the one held for the table rather than being covered by it: a registration's does.
    replacing: bool = False


def lane_task(event: object) -> LaneTask | None:
    """The task this lane would queue for ``event``, or ``None`` when it acknowledges the event untouched.

    ONE DECISION FOR THE CHECK AND THE ACT ([[XC-078]]). The lane checks the signature of exactly the events this
    selects and holds a task for exactly those, so what it acts on cannot widen past what it checks. FAILS CLOSED on
    anything it cannot read, a CloudEvent whose `data` is not an object included: events arrive off a bus and are
    client-controlled, so a malformed one is ignored rather than raised, which would turn one bad publish into a
    redelivery loop. A pass needs the table's location and a release needs its uuid, so a write naming no
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
    return LaneTask(kind="table", uuid=uuid, location=location, replacing=operation == _REGISTER)


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


def _bounded(value: object) -> str | None:
    """A string the event carries, cut to `_MAX_LOGGED_CHARS`; anything else is None."""
    if not isinstance(value, str):
        return None
    return value if len(value) <= _MAX_LOGGED_CHARS else f"{value[:_MAX_LOGGED_CHARS]}..."


class Arrivals:
    """What one replica does with the commit lane's deliveries: holds the task each admitted announcement asks for."""

    def __init__(self, settings: MaintenanceSettings, queue: TaskQueue) -> None:
        self._settings = settings
        self._queue = queue
        #: This replica's key reader, built on first use and kept so its cache outlives one delivery.
        self._keys: PublishedKeys | None = None

    async def hold_task(self, body: bytes) -> ArrivalOutcome:
        """Put the task the announcement ``body`` asks for on the work queue (:func:`lane_task`), now or held, and
        answer how the delivery is acknowledged.

        A table's pass is held a delay out, unless one is held already, which covers this event; a registration's
        replaces the one held, and is never covered. A drop's release is queued now, unless one is queued already. An event the lane leaves untouched, a write no table's claim holds and
        an event no listed signer's key verifies are acknowledged and hold nothing: none of those is repaired by a
        redelivery. Handled as a span of the trace the announcement was published under, so the task it holds is linked
        to the commit that asked for it.

        Raises:
            ValueError: ``body`` is no event envelope.
            OSError: The keys a signer publishes, or the claim at the write's location, could not be read.
            QueueUnavailableError: The bus did not take the task.
        """
        envelope = envelope_of(body)
        if envelope is None:
            raise ValueError("a commit announcement's body is no event envelope")
        arrived = envelope.get("data", envelope)
        lane = lane_task(arrived)
        if lane is None:
            return "ignored"
        with processing(self._settings.lineage_topic, envelope, consumer=ARRIVALS_CONSUMER):
            verdict = await self._judged(arrived, event_id=envelope.get("id"))
            if verdict.retry:
                raise ConnectionError("the keys the announcement's signer publishes could not be read")
            if not verdict.act:
                return "refused"
            return await self._held(lane)

    async def _held(self, lane: LaneTask) -> ArrivalOutcome:
        uuid = lane.uuid or await run_in_threadpool(self._claimed_uuid, lane.location)
        if uuid is None:
            return "ignored"
        task = Task(kind=lane.kind, uuid=uuid, location=lane.location)
        if task.kind == "table":
            at = datetime.now(UTC) + timedelta(seconds=self._settings.commit_pass_delay_seconds)
            held = await self._queue.follow_up(task, at, replacing=lane.replacing)
        else:
            held = await self._queue.enqueue(task)
        return "held" if held else "covered"

    def _claimed_uuid(self, location: str) -> str | None:
        """The uuid of the table holding ``location``'s claim, for an event that names none; ``None`` when no table does,
        or its claim cannot be read as one. Blocking.

        Raises:
            OSError: The store refused, or could not be reached for, the claim's read.
        """
        try:
            held = location_claims.holder(claim_store(self._settings), location)
        except ValueError as exc:
            log.warning("maintenance_arrival_claim_unreadable", extra={"location": _bounded(location), "error": str(exc)[:_MAX_LOGGED_CHARS]})
            return None
        except (ClientError, BotoCoreError, StoreCredentialUnavailableError) as exc:
            raise OSError(f"the claim at {_bounded(location)} could not be read: {exc}") from exc
        return held.uuid if held is not None else None

    async def _judged(self, arrived: dict[str, Any], *, event_id: object) -> DoorVerdict:
        """The lane's verdict on an event it would hold a task for, counted and logged whenever it is anything but a
        pass.

        Over the event exactly as it arrived, never a re-serialisation: the producer signed the document it published.
        Off answers before any key is read; otherwise the check reads the store through a blocking client, so it runs in
        the threadpool.
        """
        mode = self._settings.signature_doors
        if mode is DoorMode.OFF:
            return DoorVerdict(act=True)
        if self._keys is None:
            self._keys = PublishedKeys(published_key_fetch())
        claim = signature_of(arrived)
        source = self._keys.for_event(claim.kid if claim else None)
        signers, delegators = self._settings.event_signers, self._settings.event_delegators
        verdict = await run_in_threadpool(judge, lambda: verify_signature(arrived, source=source, signers=signers, delegators=delegators), mode)
        run = arrived.get("run")
        subject = {
            "door": ARRIVAL_DOOR,
            "identity": _bounded(claim.identity) if claim else None,
            "event_id": _bounded(event_id),
            "run_id": _bounded(run.get("runId")) if isinstance(run, dict) else None,
        }
        if verdict.refused is not None:
            record_signature_refused(door=ARRIVAL_DOOR, reason=verdict.refused)
            log.warning("maintenance_signature_refused", extra={**subject, "reason": verdict.refused})
        elif verdict.observed is not None:
            record_signature_would_refuse(door=ARRIVAL_DOOR, reason=verdict.observed)
            log.warning("maintenance_signature_would_refuse", extra={**subject, "reason": verdict.observed})
        elif verdict.retry:
            log.warning("maintenance_signature_keys_unavailable", extra=subject)
        return verdict
