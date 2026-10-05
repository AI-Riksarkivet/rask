"""The cascade head that fires on a PUBLICATION, not on a table create (§ D2, B8).

`/bronze-arrival` fires on a lineage write event. That is the wrong signal for two reasons the
ruling makes explicit:

* **A commit is not a publication (D-R1).** A lineage write says bytes landed; it says nothing about
  whether the quality gate passed them. Waking the cascade on it means the cascade can move data the
  gate has not accepted — the exact hole `published` exists to close.
* **It names a table, not a delta (D-R3).** "This table changed" cannot express WHICH rows are new,
  so a consumer must rescan the tier or invent its own bookmark. Measured consequence: a table's
  SECOND arrival wakes nothing useful, which is defect B8.

So this head consumes the catalog's `table_published` control event and drives the cascade from it,
propagating `{from_version, to_version}` onto the stage trigger. The stage runner then reads exactly the
rows the publication added.

**The tag remains the truth; this is the wake-up.** The event may be lost without consequence — a
consumer can always ask the catalog what `published` points at — which is why this path is allowed
to be best-effort in a way the tag is not.

`project` is carried because the stage runner cannot resolve its tier URIs without it: `handle_stage` falls
back to `MEDALLION_FROM_URI`/`MEDALLION_TO_URI`, which are empty by default, and then SKIPS its
compute path entirely (`transform.py:186-192`). That silent skip is the other half of B8 — the
cascade "ran" and moved nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from service_kit import dapr_publish
from service_kit.lakehouse.naming import CATALOG_DELIMITER


log = logging.getLogger(__name__)

_SUCCESS = {"status": "SUCCESS"}
_RETRY = {"status": "RETRY"}

#: The action this head fires on. Anything else on the control topic — grants, warehouses, table
#: creates — is a governance notice, not a readiness one, and must drive nothing.
PUBLISHED_ACTION: Final[Literal["table_published"]] = "table_published"

#: The catalog's identifier delimiter — the estate-wide `CATALOG_DELIMITER`, not a medallion knob.
#: The value belongs to the CATALOG's identifier grammar; a medallion-side knob for it would be a
#: second source of truth for someone else's format.
DELIMITER = CATALOG_DELIMITER


def _table_name(object_id: str, delimiter: str) -> str | None:
    """The table's own name from `table:<namespace>$<name>` — the LAST segment, at any nesting depth.

    THE TENANT IS DELIBERATELY NOT DERIVED HERE. This returned `(segments[0], segments[-1])` and
    called the first half the project, but a catalog namespace is project-QUALIFIED with a hyphen
    (`acme-bronze`), so for every id the estate actually produces that was the namespace. There is no
    better split either: `PROJECT_PATTERN` permits `-` inside a project id, so `acme-bronze` is
    ambiguous between project `acme` and project `acme-bronze`. It arrives on the event instead —
    `extra.project`, resolved by the catalog through the warehouse binding.
    """
    if not object_id.startswith("table:"):
        return None
    identifier = object_id.removeprefix("table:")
    if delimiter not in identifier:
        return None
    return identifier.split(delimiter)[-1] or None


def _source_namespace(object_id: str, delimiter: str, project: str) -> str | None:
    """The published table's own namespace, de-qualified of its tenant — `acme-silver` -> `silver`.

    Sound only because the catalog STATES the project on the event: `PROJECT_PATTERN` permits hyphens,
    so nothing here could recover `acme` from `acme-silver` by splitting.
    """
    if not object_id.startswith("table:"):
        return None
    identifier = object_id.removeprefix("table:")
    if delimiter not in identifier:
        return None
    namespace = identifier.rsplit(delimiter, 1)[0]
    if project and namespace.startswith(f"{project}-"):
        namespace = namespace[len(project) + 1 :]
    return namespace or None


def _originator(extra: dict[str, Any]) -> str:
    """The PERSON this publication is for — ``extra.originator``, or ``""`` when it is for nobody.

    READ, NEVER DERIVED from the event's ``actor``, because the actor is wrong for the only path that
    matters: under one door a stage runner does not publish the next stage's trigger, it publishes its
    output to the catalog — authenticating AS ITSELF — so the actor of a cascade publication is
    ``user:service-<daprAppId>``. Carried verbatim, a gold stage that fails an hour later addresses an
    inbox actor named after a stage runner: role-shaped, unread by anyone, and indistinguishable from a
    delivery.

    The catalog resolves it instead (`publication_originator`), because it is the only component that
    knows whether its caller was a person or a service, and resolving it once at the choke point is
    what stops two consumers deriving it differently. It arrives already checked for the shapes that
    name nobody, so this is a read: the value is a bare sub or the key is absent.

    A head deployed ahead of the catalog that fills the field reads nothing and carries nothing — the
    cascade reaches its author and no originator, which is the pre-fix behaviour minus the service
    name. Degrading to NO audience rather than the WRONG one is the same direction trap 3's project
    resolution takes, and for the same reason: a miss is a miss, a wrong address looks delivered.
    """
    originator = extra.get("originator")
    return originator.strip() if isinstance(originator, str) else ""


def build_stage_trigger(*, object_id: str, event_id: str, extra: dict[str, Any]) -> dict[str, Any] | None:
    """The stage trigger for one published edge — THE shape, in one place.

    Two producers mint this: the `table_published` subscription below, and the operator's re-run verb.
    They must agree exactly, because every field is read by a different guard on the stage runner and a
    mismatch is not a loud failure but a wrong one — the wrong lane DROPped as another's, the wrong
    delta range, or a composed path instead of the bytes the catalog actually vended. `stage_stamp.py`
    exists because two hand-maintained copies of one transform drifted into different schemas; its
    docstring is the rule this follows: *a mirror maintained by hand is a mirror that drifts*.

    Returns ``None`` when the object is not a table id (no `table:` prefix, no delimiter, or an empty
    namespace or table). The CALLER decides what that means — the subscription acks it, while the re-run
    verb answers the operator's malformed input with a 400. Deciding here would force one of those
    answers on both.
    """
    table = _table_name(object_id, DELIMITER)
    if table is None:
        return None
    project = str(extra.get("project") or "")
    # The LANE is `<tier>$<table>`, the same string for every tenant — `transform.py` compares the
    # arrived name against the raw `settings.from_dataset`, and the tenant travels separately in
    # `project`. Publishing the CATALOG identifier as the lane once meant `acme$events` was compared
    # against `bronze$events` and every tenant's publication DROPped as another lane's.
    source = _source_namespace(object_id, DELIMITER, project)
    if not source:
        return None
    trigger: dict[str, Any] = {
        "token": event_id,
        "dataset": f"{source}{DELIMITER}{table}",
        "namespace": source,
        # THE RANGE (D-R3). A consumer resolves it by the change-feed consumer rule
        # (`catalog.services.changes`) and keeps no bookmark. `from_version` is None on a dataset's first publication, meaning
        # "everything up to `to`" — carried as-is rather than coerced to 0, because "no prior
        # publication" and "published from version 0" are different claims.
        "from_version": extra.get("from_version"),
        "to_version": extra.get("to_version"),
        # The catalog's VENDED location. Carried so the stage runner reads the table that was actually
        # written instead of composing a path of its own (I2).
        "from_uri": extra.get("location"),
    }
    # Read, never derived. Absent means a single-tenant estate (or a catalog predating the field), and
    # omitting it is what the stage runner reads as "no tenant" — `""` would be refused as garbage.
    if project:
        trigger["project"] = project
    # THE BATCH IDENTITY, carried across the tier boundary (§8 change 9). `token` is minted per
    # publication, so without this every tier is a fresh run with nothing joining it to the ingest
    # that started the batch.
    cascade_id = str(extra.get("cascade_id") or "")
    if cascade_id:
        trigger["cascade_id"] = cascade_id
    # THE HUMAN, beside the batch identity — the two fields a publication-driven cascade would
    # otherwise lose at exactly the same hop. Omitted rather than blank: `""` is carried to an inbox
    # actor named "".
    originator = _originator(extra)
    if originator:
        trigger["originator"] = originator
    return trigger


class PublicationArrival(BaseModel):
    """A delivery the head acts on: the control event as it arrived, and the trigger it wakes a lane with.

    ``event`` is the CloudEvent's data with its members unchanged, never a re-serialisation or a model dump, because it
    is the document a door verifies the catalog's signature over ([[XC-078]]).
    """

    model_config = ConfigDict(frozen=True)

    event: dict[str, Any]
    object_id: str
    trigger: dict[str, Any]
    topic: str


def publication_arrival_of(event: Any, settings: Any) -> PublicationArrival | None:
    """What the head acts on in this delivery, or ``None`` for a delivery it acknowledges and ignores.

    Only a `table_published` whose table maps to a declared lane is acted on. Anything else is acknowledged, so an
    unrelated control event is not redelivered forever: a head that retries on events it will never handle turns one
    unparseable message into a permanent hot loop.
    """
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        return None  # not a parseable control event

    if data.get("action") != PUBLISHED_ACTION:
        return None  # a governance notice, not a readiness one

    object_id = str(data.get("object_id") or "")
    extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
    trigger = build_stage_trigger(object_id=object_id, event_id=str(data.get("event_id") or ""), extra=extra)
    if trigger is None:
        log.debug("medallion_publication_not_a_lane", extra={"object_id": data.get("object_id")})
        return None
    topic = settings.transform_routes.get(str(trigger["namespace"]))
    if not topic:
        log.debug("medallion_publication_not_a_lane", extra={"object_id": data.get("object_id"), "source": trigger["namespace"]})
        return None
    return PublicationArrival(event=data, object_id=object_id, trigger=trigger, topic=topic)


async def handle_publication(dapr: Any, settings: Any, event: dict[str, Any]) -> dict[str, str]:  # noqa: ANN401 — the Dapr client + settings seams
    """Turn a `table_published` control event into a stage trigger carrying the RANGE.

    The route runs the two halves with the signature door between them (`medallion.api.bronze_arrival`):
    :func:`publication_arrival_of` decides whether the head acts on the delivery, and :func:`fire_publication` acts.
    """
    arrival = publication_arrival_of(event, settings)
    if arrival is None:
        return _SUCCESS
    return await fire_publication(dapr, settings, arrival)


async def fire_publication(dapr: Any, settings: Any, arrival: PublicationArrival) -> dict[str, str]:
    """Publish the stage trigger for one publication. Only a publish OUTAGE returns RETRY: the one failure a redelivery can fix."""
    trigger = arrival.trigger
    landed = await dapr_publish.publish_json(
        dapr,
        pubsub_name=settings.pubsub,
        topic_name=arrival.topic,
        payload=trigger,
        timeout_seconds=settings.publish_timeout_seconds,
        failure_event="medallion_publication_trigger_failed",
        # The TOKEN beside the object: `object_id` names the catalog object and cannot find the run, so the token is
        # what joins a failed publication trigger to its cascade.
        context={"token": trigger["token"], "object_id": arrival.object_id},
    )
    if not landed:  # a publish outage is retryable; nothing else here is
        return _RETRY

    log.info(
        "medallion_publication_trigger",
        extra={"dataset": trigger["dataset"], "from_version": trigger["from_version"], "to_version": trigger["to_version"]},
    )
    return _SUCCESS
