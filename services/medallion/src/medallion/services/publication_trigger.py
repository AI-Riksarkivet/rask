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
propagating `{from_version, to_version}` onto the stage trigger. The mover then reads exactly the
rows the publication added.

**The tag remains the truth; this is the wake-up.** The event may be lost without consequence — a
consumer can always ask the catalog what `published` points at — which is why this path is allowed
to be best-effort in a way the tag is not.

`project` is carried because the mover cannot resolve its tier URIs without it: `handle_stage` falls
back to `MEDALLION_FROM_URI`/`MEDALLION_TO_URI`, which are empty by default, and then SKIPS its
compute path entirely (`transform.py:186-192`). That silent skip is the other half of B8 — the
cascade "ran" and moved nothing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from service_kit import dapr_publish


log = logging.getLogger(__name__)

_SUCCESS = {"status": "SUCCESS"}
_RETRY = {"status": "RETRY"}

#: The action this head fires on. Anything else on the control topic — grants, warehouses, table
#: creates — is a governance notice, not a readiness one, and must drive nothing.
PUBLISHED_ACTION = "table_published"

#: The catalog's identifier delimiter. Hardcoded here rather than read from medallion settings
#: because medallion has none — the value belongs to the CATALOG's identifier grammar, and inventing
#: a medallion-side knob for it would be a second source of truth for someone else's format.
DELIMITER = "$"


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


#: Principals that name no PERSON. A wildcard is a statement about everyone and therefore about no
#: one; a userset addresses a group; a bare prefix addresses nothing at all. Carrying any of them
#: writes into an inbox actor literally named `*` — worse than silence, because it looks delivered.
_NOT_A_PERSON = frozenset({"", "*", "user:*"})


def _publisher(event: dict[str, Any]) -> str:
    """The verified sub of the person who published, or ``""``.

    This head is the last place that identity exists — by the time a later stage fails the request is
    gone and the mover authors as a chart role literal. Only a ``user:`` principal is an address; a
    userset or wildcard is refused rather than carried.
    """
    actor = event.get("actor")
    if not isinstance(actor, str) or actor in _NOT_A_PERSON or not actor.startswith("user:"):
        return ""
    sub = actor[len("user:") :].strip()
    return "" if sub in _NOT_A_PERSON or "#" in sub else sub


async def handle_publication(dapr: Any, settings: Any, event: dict[str, Any]) -> dict[str, str]:  # noqa: ANN401 — the Dapr client + settings seams
    """Turn a `table_published` control event into a stage trigger carrying the RANGE.

    Acks (`SUCCESS`) anything it does not act on, so an unrelated control event is not redelivered
    forever — a head that retries on events it will never handle turns one unparseable message into
    a permanent hot loop. Only a publish OUTAGE returns RETRY, because that is the one failure a
    redelivery can fix.
    """
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        return _SUCCESS  # not a parseable control event

    if data.get("action") != PUBLISHED_ACTION:
        return _SUCCESS  # a governance notice, not a readiness one

    table = _table_name(str(data.get("object_id") or ""), DELIMITER)
    if table is None:
        return _SUCCESS
    # The LANE is `<tier>$<table>`, the same string for every tenant — `transform.py` compares the
    # arrived name against the raw `settings.from_dataset`, and the tenant travels separately. This
    # head published the CATALOG identifier as the lane once, so `acme$events` was compared against
    # `bronze$events` and every tenant's publication was DROPped as another lane's.
    extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
    project = str(extra.get("project") or "")
    # WHICH LANE was published. Undeclared namespaces are acked and driven nowhere: a table outside
    # the cascade is published all the time, and waking bronze for it fires compute no lane owns.
    source = _source_namespace(str(data.get("object_id") or ""), DELIMITER, project)
    topic = settings.transform_routes.get(source or "")
    if not topic:
        log.debug("medallion_publication_not_a_lane", extra={"object_id": data.get("object_id"), "source": source})
        return _SUCCESS
    trigger: dict[str, Any] = {
        "token": str(data.get("event_id") or ""),
        # The LANE, tier-qualified from the medallion's own setting — the same string for every
        # tenant, which is what makes the mover's discriminator work at all.
        "dataset": f"{source}{DELIMITER}{table}",
        "namespace": source,
        # THE RANGE (D-R3). A consumer resolves it with `_row_created_at_version > from AND <= to`
        # and keeps no bookmark. `from_version` is None on a dataset's first publication, meaning
        # "everything up to `to`" — carried as-is rather than coerced to 0, because "no prior
        # publication" and "published from version 0" are different claims.
        "from_version": extra.get("from_version"),
        "to_version": extra.get("to_version"),
        # The catalog's VENDED location for the published table. Carried so the mover reads the table
        # that was actually written instead of composing a path of its own (I2).
        "from_uri": extra.get("location"),
    }
    # Read, never derived. Absent means a single-tenant estate (or a catalog that predates the field),
    # and omitting it is what the mover reads as "no tenant" — `""` would be refused as garbage.
    if project:
        trigger["project"] = project
    # THE BATCH IDENTITY, carried across the tier boundary (§8 change 9). This is the hop that used to
    # lose it: `token` above is minted from the publication event id, so without this every tier is a
    # fresh run with nothing joining it to the ingest that started the batch.
    cascade_id = str(extra.get("cascade_id") or "")
    if cascade_id:
        trigger["cascade_id"] = cascade_id
    # Omitted rather than blank: `""` would be carried to an inbox actor named "".
    publisher = _publisher(data)
    if publisher:
        trigger["originator"] = publisher

    try:
        await dapr_publish.publish_event(
            dapr,
            timeout_seconds=settings.publish_timeout_seconds,
            pubsub_name=settings.pubsub,
            topic_name=topic,
            data=json.dumps(trigger),
            data_content_type="application/json",
        )
    except Exception as exc:  # noqa: BLE001 — a publish outage is retryable; nothing else here is
        log.warning("medallion_publication_trigger_failed", extra={"object_id": data.get("object_id"), "error": str(exc)})
        return _RETRY

    log.info(
        "medallion_publication_trigger",
        extra={"dataset": trigger["dataset"], "from_version": trigger["from_version"], "to_version": trigger["to_version"]},
    )
    return _SUCCESS
