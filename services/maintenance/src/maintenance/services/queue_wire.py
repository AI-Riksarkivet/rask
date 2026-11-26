"""What maintenance's work queue is on the bus (ADR 0198 §2): the streams, durable consumers and bucket it is held in, each
declared by the chart's stream Job (`chart/templates/nats-stream-job.yaml`) under the names here, and a task as its
message carries it, with the subjects it lives on.

Two streams, because a stream holding message schedules may not discard new messages (ADR-51) while the queue must refuse
a second task for a table that has one:

* ``MAINTENANCE_TASKS`` (work queue, one message per subject, a new one discarded): ``maintenance.task.<kind>.<uuid>``.
  A task already queued, or in flight, refuses another, so a burst of commits makes one pass, and a table is never
  passed twice at once.
* ``MAINTENANCE_SCHEDULES`` (work queue, schedules allowed): ``maintenance.schedule.<kind>.<uuid>`` holds when the task
  is next due, a newer one replacing it, and fires into ``maintenance.due.<kind>.<uuid>``, which the worker moves onto
  the queue (:meth:`maintenance.services.queue_worker.Worker.run_due`); its ack deletes the fired message, and no
  consumer reads a held one. Measured on nats-server 2.15.2 (2026-11-09): a replaced ``@at`` fires once, with the newer
  body; one already past fires at once.

A commit's follow-up (:meth:`maintenance.services.queue.TaskQueue.follow_up`) is held beside the table's schedule, on
``maintenance.schedule.followup.<kind>.<uuid>``, and never replaces the schedule a pass writes.
"""

from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lakehouse.records.commit_records import is_canonical_uuid


TASKS_STREAM: Final = "MAINTENANCE_TASKS"
SCHEDULES_STREAM: Final = "MAINTENANCE_SCHEDULES"
#: The store's change events (:mod:`maintenance.services.store_events`), which the store publishes on ``store.events``.
STORE_EVENTS_STREAM: Final = "STORE_EVENTS"
#: The stream every governed write is announced on, whose announcements the commit lane reads
#: (:mod:`maintenance.services.arrival`).
ARRIVALS_STREAM: Final = "LINEAGE"
#: The chart's durable pull consumers: on the tasks, on the fired schedules, on the store's events, and on the commit
#: announcements.
TASK_CONSUMER: Final = "maintenance-worker"
DUE_CONSUMER: Final = "maintenance-due"
STORE_EVENTS_CONSUMER: Final = "maintenance-store-events"
ARRIVALS_CONSUMER: Final = "maintenance-arrival"

#: The bus user the queue is worked as, which names its connection, and the prefix of that user's reply subjects
#: (`_INBOX_<user>`, as `lakehouse.events.bus` names each user's), so no other bus user reads the tasks it fetches or the
#: answers it gets.
BUS_USER: Final = "maintenance-worker"
INBOX_PREFIX: Final = f"_INBOX_{BUS_USER}"
#: The key-value bucket holding the claim a pass takes on its table (the chart's stream Job declares it, its max age the
#: consumer's ack wait).
CLAIMS_BUCKET: Final = "MAINTENANCE_CLAIMS"
#: The shortest interval a table's orphan check is held at, in hours: a check that found residue, and a check whose
#: schedule was lost, is held this far out (owner, 2026-11-09: a one-day floor).
ORPHAN_CHECK_FLOOR_HOURS: Final = 24
#: The token a follow-up's subjects carry after their area.
_FOLLOW_UP: Final = "followup"

type TaskKind = Literal["table", "trash", "audit", "scan", "orphans"]
#: The kinds a schedule of its own holds per table or trash record, which :meth:`maintenance.services.queue.TaskQueue.held` reads.
type HeldKind = Literal["table", "trash", "orphans"]
type Area = Literal["task", "schedule", "due"]


class Task(BaseModel):
    """One unit of maintenance work, as its message carries it."""

    model_config = ConfigDict(frozen=True)

    kind: TaskKind
    #: The table's uuid, or the trashed object's; empty for the audit and the scan, of which there is one each.
    uuid: str = ""
    #: The table's location as its claim records it, for a ``table`` task.
    location: str = ""
    #: The trashed object's kind, for a ``trash`` task.
    trashed: Literal["table", "namespace"] = "table"
    #: The interval an ``orphans`` check was held at, in hours: what its next interval is computed from. It rides the
    #: schedule's body, so a lost schedule loses it, and the check is held at the floor again.
    interval_hours: int = Field(default=ORPHAN_CHECK_FLOOR_HOURS, ge=1)

    @model_validator(mode="after")
    def _names_what_its_subject_is_built_from(self) -> Self:
        """The uuid is a token of the task's subject and its claim's key, where a dot or a wildcard would name another
        task's: the audit and the scan carry none, and every other kind carries a uuid."""
        if self.kind in ("audit", "scan"):
            if self.uuid:
                raise ValueError(f"a {self.kind} task names no object, and this one names {self.uuid!r}")
        elif not is_canonical_uuid(self.uuid):
            raise ValueError(f"a {self.kind} task names its object by uuid, and {self.uuid!r} is none")
        return self

    def ended(self) -> Task:
        """This task as its next try is held after a worker ended it: an orphan check starts again at the floor."""
        return self.model_copy(update={"interval_hours": ORPHAN_CHECK_FLOOR_HOURS}) if self.kind == "orphans" else self

    def subject(self, area: Area) -> str:
        """Where this task's message lives: on the queue, held as a schedule, or fired from one."""
        return f"maintenance.{area}.{self.kind}" if self.kind in ("audit", "scan") else f"maintenance.{area}.{self.kind}.{self.uuid}"

    def follow_up_subject(self, area: Literal["schedule", "due"]) -> str:
        """Where this task's follow-up is held, or fired: beside its schedule, under a subject of its own."""
        return f"maintenance.{area}.{_FOLLOW_UP}.{self.kind}.{self.uuid}"
