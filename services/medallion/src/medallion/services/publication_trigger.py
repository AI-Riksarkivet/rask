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


def _split_object_id(object_id: str, delimiter: str) -> tuple[str, str] | None:
    """`table:ns$name` -> `(ns, name)`.

    The control event names the object canonically, which is the only identifier every consumer
    already agrees on. Anything not shaped like a table id is not ours to act on.
    """
    if not object_id.startswith("table:"):
        return None
    identifier = object_id.removeprefix("table:")
    if delimiter not in identifier:
        return None
    namespace, _, dataset = identifier.partition(delimiter)
    return (namespace, dataset) if namespace and dataset else None


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

    parts = _split_object_id(str(data.get("object_id") or ""), DELIMITER)
    if parts is None:
        return _SUCCESS
    namespace, dataset = parts

    extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
    trigger: dict[str, Any] = {
        "token": str(data.get("event_id") or ""),
        "dataset": f"{namespace}{DELIMITER}{dataset}",
        "namespace": namespace,
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
    # The tenant, without which the mover cannot resolve its tier roots and silently skips compute.
    # The namespace IS the project in this estate's hierarchy (project > warehouse > namespace).
    trigger["project"] = namespace

    try:
        await dapr_publish.publish_event(
            dapr,
            timeout_seconds=settings.publish_timeout_seconds,
            pubsub_name=settings.pubsub,
            topic_name=settings.bronze_topic,
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
