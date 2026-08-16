"""What an inbox stores, and what crosses the actor boundary.

**Pointers, never payload copies.** A stored row names a notification and this subject's relationship
to it — nothing else. The body a reader eventually sees is fetched at render time through the governed
path, which is what keeps a revoked grant from being readable out of somebody's inbox and what keeps
this store from becoming a second, ungoverned copy of the lakehouse's own state.

**Every model here is frozen.** A record that has crossed a sidecar hop is a value, not a workspace,
and the actor's central invariant — the unread count is DERIVED from the rows on every mutation —
only holds if a row cannot be edited in place behind it.

The id scheme is `run_id@STATE`, and it is not ours to choose: the shared bell already keys
seen/dismissed by it (`frontend/packages/ui/src/lib/runs/run-status.ts` `runNotificationId`), which is
what makes dismissing "started" still let "failed" through.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator


#: The hard ceiling on one page, shared by the actor's own validation and the route's `le=` bound so
#: the two cannot drift into a route that accepts a page the store refuses to serve.
INBOX_PAGE_LIMIT_MAX = 100


def _as_utc(value: datetime) -> datetime:
    """Normalise to an aware UTC instant.

    The feed's order is a `(occurred_at, notification_id)` comparison, and comparing an aware instant
    to a naive one raises rather than mis-sorts — so a producer that stamped a naive timestamp would
    take down paging for whoever received it. Normalising at the boundary makes the ordering total.
    """
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


#: An instant that is always aware and always UTC — see :func:`_as_utc`.
UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class NotificationReason(StrEnum):
    """Why this subject was told — one member per targeting source.

    Stored rather than inferred because it is what a delivery re-check keys on: "you are the author"
    and "you watch the project" are different claims about the same run, and a row that does not say
    which one it rode in on cannot be re-checked against the right rule later. The v3 members are the
    sharper case — a governance row is checked against NO object rule at all, because being named IS
    the targeting, and a reader that could not tell it apart from a run row would have to guess.
    """

    #: v1 — the run's verified author. Needs no registry and no permission.
    AUTHOR = "author"
    #: v2 — a `project#member`-gated watch on the run's project.
    WATCH = "watch"
    #: v3 — this subject was NAMED by a governance act (`extra.subject`).
    GRANT_ADDED = "grant_added"
    GRANT_REVOKED = "grant_revoked"
    #: The read-path fallback for a reason this build does not have a name for. NEVER written by an
    #: ingress — `NotificationDelivery` refuses an unknown reason at the door — and reachable only when
    #: reading back state a NEWER build stored. See `InboxPointer._tolerate_a_newer_vocabulary`.
    UNKNOWN = "unknown"
    #: v5 — the human whose work a SERVICE-authored run is doing. The medallion's movers author with a
    #: chart role literal (`data_eng`/`analyst`/`htr`/`ray`), which is a truthful statement about who ran
    #: the stage and a useless inbox address — so a failed cascade reached nobody. Distinct from AUTHOR
    #: rather than a substitution: the mover really did run it, and overwriting attribution to fix
    #: targeting would trade one wrong answer for another.
    ORIGINATOR = "originator"
    #: v4 — annotation work. Same targeting rule as the grant pair (the event NAMES its subject), one
    #: rung down: the audience is the ASSIGNEE, never the manager whose click produced the event.
    TASK_ASSIGNED = "task_assigned"
    TASK_UNASSIGNED = "task_unassigned"
    #: v6 — the rest of the departure edges. DISTINCT members rather than a reuse of TASK_UNASSIGNED
    #: because the panel renders the reason as each row's user-visible label: "unassigned" is simply
    #: the wrong sentence for reviewed work coming back, or for an item being discarded outright.
    #: `task_changes_requested` covers both review-side returns (a reviewer's `request_changes` and a
    #: manager's `reopen`) — from the submitter's side those are one fact.
    TASK_CHANGES_REQUESTED = "task_changes_requested"
    TASK_DROPPED = "task_dropped"


class InboxFilter(StrEnum):
    """The inbox list filter. `unread` is what the badge counts; `all` is what the panel shows.

    Neither includes dismissed rows — dismissal is the reader saying "not this one", and the shared
    component's own `visibleRuns` drops them before it counts anything.
    """

    UNREAD = "unread"
    ALL = "all"


def notification_id(run_id: str, state: str) -> str:
    """`run_id@STATE` — the id the bell already keys seen/dismissed by.

    Derived here rather than at each producer so the backend and the component cannot disagree about
    what "the same notification" is; `tests/test_models.py` pins the shape against the frontend helper.
    """
    return f"{run_id}@{state.upper()}"


class NotificationDelivery(BaseModel):
    """One notification as the ingress hands it to a subject — carrying no read state.

    Split from :class:`InboxPointer` so that `seen`/`dismissed` are *unspeakable* on the delivery
    path: read state is the subject's, minted by the actor, and a replayed or forged delivery must not
    be able to arrive pre-read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    notification_id: str = Field(min_length=1)
    reason: NotificationReason
    #: The governed object the render and delivery checks run against — a dataset name, checked as
    #: `table:<object_id>` (the type `LINEAGE_FGA_OBJECT_TYPE` defaults to and nothing overrides).
    object_id: str = Field(min_length=1)
    #: The producer's OWN run id (lineage's `RunStatus.source_run_id`), which is what its detail doors
    #: answer to. The graph run id is a derived uuid5 and links to nothing.
    source_run_id: str | None = None
    #: The lineage feed's sequence number when the notification arrived over the reconciler rather than
    #: the bus. Declared now though only one ingress path exists: adding a field to rows that are
    #: already stored means schema drift, and drift on this record reads as *unreadable* (below), not
    #: as absent — a migration nobody wants in exchange for one line today.
    event_seq: int | None = None
    occurred_at: UtcDatetime


class InboxPointer(NotificationDelivery):
    """A delivery plus this subject's relationship to it — the stored row.

    **READ-TOLERANT, unlike the delivery it extends, and the asymmetry is the point.**
    `NotificationDelivery` is wire input from an ingress and stays strict: an unknown reason there is a
    producer/consumer mismatch to fix, and `seen` must remain unspeakable so a forged delivery cannot
    arrive pre-read. THIS is state the service wrote itself and may read back under an OLDER build —
    every rollback and every mixed-version rollout does exactly that, which is routine rather than a
    fault.

    Strict here cost a live outage: three reasons were added, rows carrying them landed in the durable
    actor state, the deployment rolled back, and the older enum turned them into
    `ValidationError: 4 validation errors for InboxRows` — surfaced as `InboxUnreadable` and a **503 for
    the whole inbox**. Not a missing row: the badge went blank and every other notification that
    subject had became unreachable, because one list validation is all-or-nothing.

    **The tolerance is the REASON VALUE only, and `extra="forbid"` deliberately stays.** They look like
    one hazard and are not: an unknown FIELD may be another subject's data, which is why the forbid is
    a containment guard (`test_inbox_leak_containment`) and why drift there must read as UNREADABLE
    rather than be silently dropped — a filter is something a later route can forget. An unknown reason
    VALUE arrives on a field this model already declares, carries nothing foreign, and describes this
    subject's own row. Refusing it protects nobody and costs the inbox.
    """

    seen: bool = False
    dismissed: bool = False
    #: Channels this notification has already been pushed to for this subject — the delivery
    #: idempotency ledger, `(event_id, subject, channel)` with the first two implied by WHERE it is
    #: stored. JetStream redelivery is at-least-once, so without it a retried event emails you twice.
    #:
    #: It rides the POINTER rather than a ledger of its own, which is what keeps it bounded: the rows
    #: are already TTL'd and compacted, so the record of "we emailed this" ages out with the thing it
    #: is about. A separate ledger would be new state with no compaction and no reason to ever shrink.
    sent: list[str] = Field(default_factory=list)

    @field_validator("reason", mode="before")
    @classmethod
    def _tolerate_a_newer_vocabulary(cls, value: object) -> object:
        """Degrade a reason this build cannot name, rather than refusing the row that carries it.

        The row still counts, still renders and can still be read or dismissed — the subject simply
        sees a generic reason for it. That is strictly better than the alternative this replaces,
        which was losing the entire inbox."""
        if isinstance(value, str) and value not in set(NotificationReason):
            return NotificationReason.UNKNOWN
        return value

    @property
    def unread(self) -> bool:
        """What the badge counts, defined exactly as the component defines it: neither read nor dismissed."""
        return not self.seen and not self.dismissed

    @classmethod
    def arriving(cls, delivery: NotificationDelivery) -> Self:
        """A freshly delivered pointer — unread by construction, never by the caller's say-so."""
        return cls(**delivery.model_dump())

    def marked_seen(self) -> Self:
        return self.model_copy(update={"seen": True})

    def marked_dismissed(self) -> Self:
        return self.model_copy(update={"dismissed": True})


class InboxCursor(BaseModel):
    """Where a page resumes: the full ordering key of the last row served.

    Both halves, because `occurred_at` alone is not unique — two runs finishing in the same
    millisecond would make the cursor skip a row or repeat one, which is the whole reason the feed
    carries a deterministic tiebreaker.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    occurred_at: UtcDatetime
    notification_id: str = Field(min_length=1)


class InboxQuery(BaseModel):
    """One page request. `limit` is required: the default page size is a setting, not a model default,
    so measuring it later is a config change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: InboxFilter = InboxFilter.ALL
    limit: int = Field(ge=1, le=INBOX_PAGE_LIMIT_MAX)
    after: InboxCursor | None = None


class InboxPage(BaseModel):
    """One page of the feed, plus the badge count for the whole inbox.

    `has_more` comes from a `limit + 1` read rather than a count: a feed has no total worth computing,
    and the caller resumes from the last row's own ordering key.
    """

    model_config = ConfigDict(frozen=True)

    pointers: list[InboxPointer]
    has_more: bool
    unread: int = Field(ge=0)


class InboxMark(BaseModel):
    """The seen seam. An empty set is a no-op rather than an error — the panel legitimately renders
    nothing, and a marking call that refuses the empty case would make the caller branch on it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    notification_ids: list[str] = Field(default_factory=list)


class InboxDismiss(BaseModel):
    """The dismiss seam — one id, matching the component's `ondismiss(notificationId, dismissed)`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    notification_id: str = Field(min_length=1)


class InboxMeta(BaseModel):
    """The SMALL partition: read on every call, written on every mutation.

    The split exists so that the badge — by far the most frequent read in the plane — never loads the
    pointer records. `rows` is here for the same reason: the read path has to know whether this inbox
    needs a compaction reminder without paying to read the rows to find out.

    `subject` is redundant with the actor id by construction, and that is the point — it is the second
    lock (see the actor's `_require_owner`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1)
    unread: int = Field(default=0, ge=0)
    rows: int = Field(default=0, ge=0)
    #: When the compaction reminder is next expected to fire. Persisted because it is the only way a
    #: later turn can tell "armed and pending" from "armed once, and the Scheduler lost it".
    compaction_due_at: UtcDatetime | None = None
    updated_at: UtcDatetime


class ChannelPrefs(BaseModel):
    """Where a subject wants to be pushed, beyond the bell.

    OFF BY DEFAULT, every channel. The bell is in-app and costs the reader nothing; email and Slack
    are interruptions someone has to ask for. An estate that mailed everyone by default would be the
    "badge that counts other people's work" failure again, arriving in an inbox that is not ours.

    An unknown channel name is not an error here — prefs are stored as a set of channel ids and the
    dispatch table is what decides which of them can actually be sent, so removing a channel from the
    build cannot make a stored preference unreadable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1)
    #: Channel ids the subject opted into (`email`, `slack`). Empty = the bell only.
    channels: list[str] = Field(default_factory=list)
    #: Where to reach them per channel — an address the subject supplied, never one this plane
    #: inferred from a token. Claim-check applies: a value here is a destination, never content.
    destinations: dict[str, str] = Field(default_factory=dict)
    #: Batch pushes into one message every N seconds instead of sending each immediately. `None` =
    #: immediate, which stays the default because the failure this plane exists to end is a MISSED
    #: failure, and a digest delays every notification to reduce the volume of the ones nobody minded.
    #:
    #: A FAILED run is deliberately NOT digestible — see `digest_defers`. Batching the one notification
    #: someone needs now, in order to batch the ones they did not, is the trade nobody wants made for
    #: them, and it is the trade a naive digest makes silently.
    digest_seconds: int | None = Field(default=None, ge=60)
    updated_at: UtcDatetime

    def digest_defers(self, notification_id: str) -> bool:
        """Whether THIS notification waits for the digest, or goes out now.

        Terminal FAILURE always goes now, whatever the digest says. The id carries the state
        (`run_id@FAIL`) because a pointer is a claim-check with no status field of its own.
        """
        if self.digest_seconds is None:
            return False
        state = notification_id.rsplit("@", 1)[-1].upper() if "@" in notification_id else ""
        return state not in {"FAIL", "FAILED", "ABORT"}


class InboxWatches(BaseModel):
    """The subject's own watch list — the THIRD partition, read rarely and written by hand.

    Separate from meta and rows for the reason the other two are separate: it is touched only by the
    settings surface and by a fan-out that is resolving one subject, never by the badge. Putting it in
    `InboxMeta` would make every unread-count read carry it.

    IT IS THE SUBJECT'S VIEW, and the project's `WatchIndexActor` is the fan-out's view — the same
    fact recorded twice, deliberately. Neither can answer the other's question: an inbox actor cannot
    enumerate a project's watchers, and a project index cannot list one subject's projects without
    scanning every project. The watch endpoint writes both, synchronously, and fails the request if
    either write fails — a half-written watch that the settings page shows and the fan-out never reads
    is the one outcome worse than a refusal.

    `subject` is the second lock, exactly as in the two partitions above.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1)
    #: Project ids, insertion-ordered. A list rather than a set because JSON has no set and the order
    #: is a free, stable tiebreaker for the settings surface that renders it.
    projects: list[str] = Field(default_factory=list)
    updated_at: UtcDatetime


class InboxRows(BaseModel):
    """The LARGE partition: the pointer records, read only when a page or a mutation needs them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1)
    pointers: list[InboxPointer] = Field(default_factory=list)
    updated_at: UtcDatetime
