"""Projecting one OpenLineage run event into a notification — pure, and the same on both lanes.

This is where "a run finished" becomes "somebody should be told", and it is deliberately a set of
functions over a parsed payload rather than anything that knows about Dapr, FastAPI or an actor: the
bus handler and the `/events` reconciler run it identically, which is what makes "the same event
arriving twice by two routes lands one pointer" a property of the projection instead of a coincidence
of two call sites.

**The payload models are declared here rather than imported.** `lineage.models.RunEvent` is the
authoritative shape, but it lives in another DEPLOYABLE — importing it would make `lineage` a declared
dependency of `notifications`, which is the thing the workspace's per-package dependency closure
exists to prevent. `lineage_kit.schemas` is a library and would be legitimate, but it is not in this
service's declared closure either, and adding a dependency to satisfy a five-field read is the wrong
trade. What IS declared here is only what the projection reads; `extra="ignore"` means a wider event
parses unchanged, and the topic's `.v1` is the promise that the fields below keep their meaning.
"""

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from notifications.models import NotificationDelivery, NotificationReason, UtcDatetime, notification_id


#: The states a person needs to be told about. RESOLVED as a product decision, not a technical
#: default: notify-on-needs-attention — failures loudest, completions second. "Your run started" tells
#: the person who clicked start nothing and is noise to everyone else, so START notifies nobody.
#:
#: `RECONCILED` is deliberately absent although lineage's own terminal index includes it: it is that
#: service's REPAIR marker for a run whose real terminal event was lost, not an outcome anyone chose,
#: and a notification for it would announce lineage's bookkeeping to a data scientist.
TERMINAL_STATES: Final[frozenset[str]] = frozenset({"COMPLETE", "FAIL", "ABORT"})

#: The run facet carrying the VERIFIED identity. Both writers that verify it — the HTTP door
#: (`enforce_author`, which overwrites whatever the body claimed with the token sub) and the catalog's
#: emitter — write `{"name": sub, "sub": sub}`.
AUTHOR_FACET: Final = "author"

#: The lakehouse run facet carrying the producer's OWN run id.
LANCE_FACET: Final = "lance"


class LineageDataset(BaseModel):
    """A dataset an event names. `namespace` is read only to be ignored: outputs are authorized the
    same way whatever they are namespaced by (writing is the direction that mutates the estate, so an
    output claiming an external namespace is not a case to make permissive)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = ""


class LineageRun(BaseModel):
    """The run half of the event: its graph id and the facet bag identity rides in."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    #: The GRAPH run id — a derived uuid5. It is what the shared bell keys `seen`/`dismissed` by
    #: (`runNotificationId` is `${run.run_id}@${state}`), so it is what the notification id is built
    #: from; the id a detail door answers to is `source_run_id` below.
    run_id: str = Field(alias="runId", min_length=1)
    facets: dict[str, Any] = Field(default_factory=dict)


class LineageRunEvent(BaseModel):
    """The subset of an OpenLineage run event this plane reads."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    event_type: str = Field(alias="eventType", min_length=1)
    #: Parsed and normalised to aware UTC at the boundary — the feed's order is an
    #: `(occurred_at, notification_id)` comparison, and a naive instant would raise on comparison
    #: rather than mis-sort, taking paging down for whoever received it.
    event_time: UtcDatetime = Field(alias="eventTime")
    run: LineageRun
    outputs: list[LineageDataset] = Field(default_factory=list)


class Notifiable(BaseModel):
    """A terminal run that has BOTH an audience and an object — the only thing worth a pointer.

    `outputs` is the run's FULL output set, not just the one named on the pointer, because the
    delivery check is a subset test over all of them (`if names <= visible`): one invisible output
    drops the row. What the pointer stores is the primary output alone, and that asymmetry is
    deliberate — see :func:`notifiable`.
    """

    model_config = ConfigDict(frozen=True)

    delivery: NotificationDelivery
    #: The verified author — v1's whole audience.
    author: str
    outputs: frozenset[str]


def is_terminal(event_type: str) -> bool:
    """Whether this state is one a person needs to be told about — see :data:`TERMINAL_STATES`."""
    return event_type.upper() in TERMINAL_STATES


def _facet_field(facets: dict[str, Any], facet: str, key: str) -> str | None:
    """One string field of a custom run facet, or `None` when the facet/key is absent or not a string.

    Tolerant on purpose: facets are an open bag on an untrusted envelope, and a producer that writes a
    number where a string belongs must cost this plane a missing notification, never a raise inside a
    subscription handler.
    """
    bag = facets.get(facet)
    if not isinstance(bag, dict):
        return None
    value = bag.get(key)
    return value if isinstance(value, str) and value.strip() else None


def author_subject(run: LineageRun) -> str | None:
    """The run's VERIFIED author, or `None`.

    `author.sub` and nothing else, and the two things it deliberately does not fall back to are the
    point. `lineage.models.RunEvent.author` prefers `author.name` and then the standard `ownership`
    JOB facet, which is right for ATTRIBUTION on a board — an external OpenLineage producer's claimed
    owner is better than no owner. It is wrong for TARGETING: `ownership` is a producer-supplied
    string nobody verified, so honouring it would let any producer put a row in a named person's
    inbox. Every estate writer that verifies the author writes `sub` (the HTTP door overwrites the
    facet with the token sub; the catalog and the medallion stamp `{name, sub}` together), so reading
    only `sub` costs nothing real and removes the ambiguity entirely.
    """
    return _facet_field(run.facets, AUTHOR_FACET, "sub")


def source_run_id(run: LineageRun) -> str | None:
    """The producer's OWN run id — what its detail doors answer to.

    The graph `runId` is a derived uuid5 and links to nothing: every ingest-board row 404'd when it
    was used as the link. `None` for runs recorded before producers stated it, which a consumer must
    render unlinked rather than guess at.
    """
    return _facet_field(run.facets, LANCE_FACET, "run_id")


def notifiable(event: LineageRunEvent) -> Notifiable | None:
    """One terminal run projected into a delivery plus the audience and objects it is checked against.

    `None` — meaning "ack it, tell nobody" — in exactly three cases, and each is a rule rather than a
    tolerance:

    * NOT A TERMINAL STATE. START and RUNNING notify nobody (:data:`TERMINAL_STATES`).
    * NO VERIFIED AUTHOR. v1's audience IS the author, so an event without one has no audience; see
      :func:`author_subject` for why an unverified name does not count as one.
    * NO OUTPUTS. A dataset-less run is dropped by the estate's governed read path when FGA is on,
      because it would otherwise pass the visibility test vacuously and disclose run/author/error to a
      caller holding no grants. This plane refuses it under FGA on AND off, one step earlier and for a
      second reason: a pointer names the object it is about, and there is no honest value for that
      field. One rule everywhere, and no configuration in which it relaxes.

    THE POINTER STORES THE PRIMARY OUTPUT WHILE THE DELIVERY CHECK RUNS OVER ALL OF THEM, and that
    gap is safe rather than merely tolerated. The pointer is a claim-check: it names `object_id`,
    which the render path re-checks, and nothing else about the run's other outputs. So a subject who
    later loses their grant on a SECOND output keeps seeing a row that discloses only the first — the
    object they can still see. The subset test is about the AUDIENCE (should you be told at all); the
    render check is about THIS ROW'S OWN OBJECT, and neither is a weaker version of the other.
    `outputs[0]` as "the run's output" is the estate's existing reading (`LineageDoc.from_run_event`
    projects exactly that).
    """
    if not is_terminal(event.event_type):
        return None
    author = author_subject(event.run)
    if author is None:
        return None
    outputs = [dataset.name for dataset in event.outputs if dataset.name]
    if not outputs:
        return None
    state = event.event_type.upper()
    return Notifiable(
        delivery=NotificationDelivery(
            # `run_id@STATE`, which for a terminal event IS lineage's own dedupe key for the feed —
            # `(run_id, event_type)`, the partial unique index that dedups a RETRY-after-partial-success
            # re-emitting the same run's COMPLETE with a fresh eventTime. So the actor's
            # idempotency-on-notification_id and the feed's natural key are the same fact, which is why
            # an event arriving on both lanes lands exactly one pointer.
            notification_id=notification_id(event.run.run_id, state),
            reason=NotificationReason.AUTHOR,
            object_id=outputs[0],
            source_run_id=source_run_id(event.run),
            occurred_at=event.event_time,
        ),
        author=author,
        outputs=frozenset(outputs),
    )
