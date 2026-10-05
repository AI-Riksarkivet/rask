"""A run IS its plan (CP-029 D-1): one document per action, written before the work is submitted, closed by its first terminal outcome.

A RECORD, NOT A REPLAYED HISTORY, which is the shape Lakekeeper runs work as (leased records plus doors): the service
that plans a run writes this document into its control root, the job reports its own terminal state through an
outcome door keyed on the same id, and a sweep resolves what no report arrived for. A record survives the planner's
restart and a job that outlives any poll, and nothing in it names an engine.

THE ID IS CONTENT-ADDRESSED. A stage's action id is `work_order.derive_idempotency_key` (stage, token, from, to,
code_version), the same value the engine submits under, so the plan, the job and the outcome are one name.

TWO OBJECTS PER RUN, under ``<control root>/_plans/<owner>/``, because the sweep must find the OPEN runs without
reading every closed one: ``runs/<id>.json`` is the document (create-if-absent, then ETag CAS through
`service_kit.lakehouse.records`), and ``open/<id>`` is an empty index entry written BEFORE the document is opened
and removed AFTER it closes. So every open plan has an entry; an entry whose document is closed or never landed is
removed by the sweep (:meth:`PlanStore.forget_stale_entry`).

CLOSING IS CAS AND THE FIRST TERMINAL WINS. The same outcome again answers idempotently; a different one is refused
(:class:`OutcomeConflictError`), because two answers to one run cannot both be true and the first already acted.

RETENTION: a closed document is kept for :data:`CLOSED_RETENTION` (14 days), long enough for a late job report or a
redelivered trigger to find it, and pruned after. The prune re-reads a document before deleting it, so the residual is
a reopen landing between that read and the delete: the reopened plan's document goes, its job's report is refused
404, and the run is resubmitted by the next delivery of its trigger. A 14-day-old run reopened in that window is the
whole exposure.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal

import pyarrow.fs as pafs
from pydantic import BaseModel, ConfigDict, Field, model_validator

from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base
from service_kit.lakehouse.record_store import delete_record, put_record
from service_kit.lakehouse.records import RecordExistsError, create_json, mutate_json, read_json
from service_kit.lakehouse.work_order import WorkOrder


log = logging.getLogger(__name__)

#: The control-root prefix every plan lives under.
PLANS_PREFIX: Final = "_plans"

#: How long a CLOSED plan is kept before the sweep prunes it.
CLOSED_RETENTION: Final = timedelta(days=14)

#: How long an open-index entry may name a document that does not exist before it is removed. The planner writes
#: the entry first, so a younger orphan may be a plan still being written.
ORPHAN_ENTRY_GRACE: Final = timedelta(minutes=10)

#: An action id or owner as it may appear in an object key: a run's 40-hex `derive_idempotency_key`, or an owner's identity.
KEY_SEGMENT_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"

#: The cap on a terminal outcome's error text. It becomes a FAIL event's message, which is published through the
#: claim-check funnel, so an upstream traceback must not size it.
ERROR_MAX_CHARS: Final = 4096


class PlanKind(StrEnum):
    """The lane a plan belongs to: which outcome door takes its report and which sweep resolves it."""

    STAGE = "stage"
    TRAIN = "train"


#: How a run ended. A run either succeeded or did not; "the job is still running" is not an outcome.
type OutcomeStatus = Literal["succeeded", "failed"]

#: Who decided the outcome: the job through its door, the sweep from the engine's status or the destination's
#: history, or an operator's terminate.
type OutcomeSource = Literal["job", "sweep", "operator"]


class RunOutcome(BaseModel):
    """A plan's terminal outcome, absent while the run is open."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: OutcomeStatus
    error: str | None = Field(default=None, max_length=ERROR_MAX_CHARS)
    #: The destination version the run's commit marker names, when one was found (`commit_marker.marked_version`).
    committed_version: int | None = Field(default=None, ge=0)
    source: OutcomeSource
    recorded_at: datetime


class PlanDocument(BaseModel):
    """One planned run: what it does, where it reports, how the sweep has seen it, and how it ended.

    ``order`` is the run's exact `WorkOrder`, kept so a resubmit or a redelivery sends the same work under the same key;
    the identity fields beside it mirror the order where one is present (a validator holds them equal), so a plan
    written without an order still parses and a reader of the published plan needs no engine's vocabulary. A training
    run's order has no source, and its plan's ``from_uri`` and ``from_id`` are empty.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str = Field(pattern=KEY_SEGMENT_PATTERN)
    run_id: str
    kind: PlanKind
    engine: str = Field(min_length=1)
    task: str
    stage: str
    from_uri: str
    from_id: str = ""
    to_uri: str
    to_id: str = ""
    #: The destination's version when the run was planned, ``None`` when it did not exist. A commit marker counts only
    #: above it.
    base_version: int | None = Field(default=None, ge=0)
    code_version: str = ""
    originator: str = ""
    on_behalf_of: str = ""
    project: str = ""
    #: The trigger that resumes the run once its job has landed, as the planner will re-parse it.
    trigger: dict[str, Any] = Field(default_factory=dict)
    submitted_at: datetime
    #: The outcome door this run's job reports to, naming the action id.
    report_url: str = ""
    #: What the engine was asked to run (the task registration's command).
    command: str = ""
    order: WorkOrder | None = None
    # The sweep's watch state, CAS-updated tick by tick.
    seen: bool = False
    unseen_ticks: int = Field(default=0, ge=0)
    resubmits: int = Field(default=0, ge=0)
    attempt: int = Field(default=1, ge=1)
    #: Whether the plan reached the control lane for this attempt; the sweep publishes one that did not.
    published: bool = False
    outcome: RunOutcome | None = None

    @model_validator(mode="after")
    def _mirrors_its_order(self) -> PlanDocument:
        if self.order is None:
            return self
        order = self.order
        expected = {
            "action_id": order.idempotency_key,
            "run_id": order.identity.run_id,
            "task": order.task,
            "stage": order.stamp.stage,
            "from_uri": order.source.uri if order.source is not None else "",
            "from_id": order.source.table_id if order.source is not None else "",
            "to_uri": order.destination.uri,
            "to_id": order.destination.table_id,
            "code_version": order.identity.code_version,
            "originator": order.identity.originator,
            "project": order.identity.project,
            "report_url": order.outcome_url,
        }
        drifted = sorted(name for name, value in expected.items() if getattr(self, name) != value)
        if drifted:
            raise ValueError(f"plan {self.action_id!r} disagrees with its own order on {drifted}")
        return self

    @classmethod
    def for_order(
        cls,
        order: WorkOrder,
        *,
        kind: PlanKind,
        engine: str,
        command: str,
        base_version: int | None,
        trigger: dict[str, Any],
        submitted_at: datetime,
        on_behalf_of: str = "",
    ) -> PlanDocument:
        """The plan of ``order``, its identity fields taken from the order so the two cannot disagree."""
        return cls(
            action_id=order.idempotency_key,
            run_id=order.identity.run_id,
            kind=kind,
            engine=engine,
            task=order.task,
            stage=order.stamp.stage,
            from_uri=order.source.uri if order.source is not None else "",
            from_id=order.source.table_id if order.source is not None else "",
            to_uri=order.destination.uri,
            to_id=order.destination.table_id,
            base_version=base_version,
            code_version=order.identity.code_version,
            originator=order.identity.originator,
            on_behalf_of=on_behalf_of,
            project=order.identity.project,
            trigger=trigger,
            submitted_at=submitted_at,
            report_url=order.outcome_url,
            command=command,
            order=order,
        )

    @property
    def is_open(self) -> bool:
        """Whether the run has no terminal outcome yet."""
        return self.outcome is None


class OutcomeConflictError(Exception):
    """A plan already closed with one outcome was given a different one; the first stands."""

    def __init__(self, plan: PlanDocument, refused: OutcomeStatus) -> None:
        self.plan = plan
        self.refused = refused
        recorded = plan.outcome.status if plan.outcome is not None else "none"
        super().__init__(f"plan {plan.action_id!r} closed {recorded}; a {refused} outcome is refused")


class PlanStore:
    """One owner's plans in a control root. Every method is BLOCKING IO; an async caller threadpools it."""

    def __init__(self, root: str, storage_options: StorageOptions, *, owner: str) -> None:
        if not root:
            raise ValueError("a plan store needs a control root: the run's plan lives there")
        if not _segment_ok(owner):
            raise ValueError(f"plan owner {owner!r} cannot name an object key")
        self.root = root.rstrip("/")
        self.storage_options = storage_options
        self.owner = owner

    def plan_uri(self, action_id: str) -> str:
        """Where the document lives: the pointer a published plan carries."""
        return f"{self.root}/{self._plan_key(action_id)}"

    def _plan_key(self, action_id: str) -> str:
        return f"{PLANS_PREFIX}/{self.owner}/runs/{action_id}.json"

    def _entry_key(self, action_id: str) -> str:
        return f"{PLANS_PREFIX}/{self.owner}/open/{action_id}"

    def create(self, plan: PlanDocument) -> tuple[PlanDocument, bool]:
        """Store ``plan`` unless a plan under its id exists; answer the stored plan and whether this call created it."""
        put_record(self.root, self.storage_options, self._entry_key(plan.action_id), {})
        try:
            create_json(self.root, self.storage_options, self._plan_key(plan.action_id), plan.model_dump(mode="json"))
        except RecordExistsError:
            stored = self.read(plan.action_id)
            if stored is None:
                raise
            if not stored.is_open:
                # The entry written above names a closed plan; the sweep would remove it, and removing it now keeps
                # the index exact for the caller that is about to decide whether to reopen.
                delete_record(self.root, self.storage_options, self._entry_key(plan.action_id))
            return stored, False
        return plan, True

    def read(self, action_id: str) -> PlanDocument | None:
        """The plan under ``action_id``, or ``None`` when there is none. A document that does not validate raises."""
        if not _segment_ok(action_id):
            return None
        found = read_json(self.root, self.storage_options, self._plan_key(action_id))
        return None if found is None else PlanDocument.model_validate(found[0])

    def update(self, action_id: str, change: Callable[[PlanDocument], PlanDocument]) -> PlanDocument:
        """Apply ``change`` to the plan under ETag CAS; ``change`` may run more than once and must be pure."""

        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            return change(PlanDocument.model_validate(raw)).model_dump(mode="json")

        return PlanDocument.model_validate(mutate_json(self.root, self.storage_options, self._plan_key(action_id), mutate))

    def close(self, action_id: str, outcome: RunOutcome) -> PlanDocument:
        """Record ``outcome`` if the plan is open; the same status again is idempotent, another raises `OutcomeConflictError`."""

        def first_wins(plan: PlanDocument) -> PlanDocument:
            if plan.outcome is None:
                return plan.model_copy(update={"outcome": outcome})
            if plan.outcome.status == outcome.status:
                return plan
            raise OutcomeConflictError(plan, outcome.status)

        closed = self.update(action_id, first_wins)
        delete_record(self.root, self.storage_options, self._entry_key(action_id))
        return closed

    def reopen(self, action_id: str, *, base_version: int | None, submitted_at: datetime) -> PlanDocument:
        """Open a FAILED plan for its next attempt: a fresh watch, the destination's version now as the marker floor."""
        put_record(self.root, self.storage_options, self._entry_key(action_id), {})

        def next_attempt(plan: PlanDocument) -> PlanDocument:
            if plan.outcome is None:
                return plan
            if plan.outcome.status != "failed":
                raise OutcomeConflictError(plan, "failed")
            return plan.model_copy(
                update={
                    "outcome": None,
                    "attempt": plan.attempt + 1,
                    "seen": False,
                    "unseen_ticks": 0,
                    "resubmits": 0,
                    "published": False,
                    "base_version": base_version,
                    "submitted_at": submitted_at,
                }
            )

        return self.update(action_id, next_attempt)

    def open_entries(self) -> list[tuple[str, datetime]]:
        """The ids the open index names, with each entry's last-modified time."""
        return [(name, mtime) for name, mtime in self._listing("open") if _segment_ok(name)]

    def forget_stale_entry(self, action_id: str, written_at: datetime, *, now: datetime) -> bool:
        """Remove an open-index entry whose plan is closed, or absent past :data:`ORPHAN_ENTRY_GRACE`; answer whether it went."""
        plan = self.read(action_id)
        stale = (plan is None and now - written_at > ORPHAN_ENTRY_GRACE) or (plan is not None and not plan.is_open)
        if stale:
            delete_record(self.root, self.storage_options, self._entry_key(action_id))
        return stale

    def prune(self, *, now: datetime) -> int:
        """Delete closed plans older than :data:`CLOSED_RETENTION`; answer how many went. An unreadable document is kept and logged."""
        pruned = 0
        for name, mtime in self._listing("runs"):
            if not name.endswith(".json") or now - mtime <= CLOSED_RETENTION:
                continue
            action_id = name.removesuffix(".json")
            try:
                plan = self.read(action_id)
            except ValueError as exc:  # not JSON, or not a plan (ValidationError is a ValueError)
                log.warning("run_plan_unreadable", extra={"owner": self.owner, "action_id": action_id, "error": str(exc)[:300]})
                continue
            if plan is not None and plan.outcome is not None and now - plan.outcome.recorded_at > CLOSED_RETENTION:
                delete_record(self.root, self.storage_options, self._plan_key(action_id))
                pruned += 1
        return pruned

    def _listing(self, child: str) -> list[tuple[str, datetime]]:
        fs, base = fs_and_base(self.root, self.storage_options)
        selector = pafs.FileSelector(f"{base}/{PLANS_PREFIX}/{self.owner}/{child}", allow_not_found=True, recursive=False)
        found: list[tuple[str, datetime]] = []
        for info in fs.get_file_info(selector):
            if info.type != pafs.FileType.File:
                continue
            mtime = info.mtime if info.mtime is not None else datetime.now(UTC)
            found.append((info.base_name, mtime if mtime.tzinfo is not None else mtime.replace(tzinfo=UTC)))
        return found


def _segment_ok(value: str) -> bool:
    return re.fullmatch(KEY_SEGMENT_PATTERN, value) is not None
