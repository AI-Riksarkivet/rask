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
    """`table:ns$name` -> `(project, table)`, at ANY nesting depth.

    The control event names the object canonically, which is the only identifier every consumer
    already agrees on. Anything not shaped like a table id is not ours to act on.

    TWO SEGMENTS ARE WANTED AND THEY ARE NOT ADJACENT, which is what the first version got wrong by
    reaching for `partition`. Catalog namespaces NEST — `namespace#parent: [warehouse, namespace]` in
    the FGA model, and the create door accepts a nested id up to ``MAX_NAMESPACE_DEPTH`` (8) — so
    `acme$bronze$pages` is the table `pages` inside the namespace `acme$bronze`:

      * the LANE takes the table's own name, the LAST segment, because the medallion lane is
        `<tier>$<unqualified-table>` and is the same string for every tenant (`transform.py:109`
        compares the arrived name against the raw `settings.from_dataset`);
      * the PROJECT takes the FIRST segment, the top of the hierarchy — a project is the top rung
        (`project > warehouse > namespace > table`) and the ingest plane creates its namespace there.

    `partition` returned the first segment for BOTH, so at depth 2 the lane became `bronze$pages`
    and the published trigger read `bronze$bronze$pages` — a lane no mover matches, which
    `transform.py` DROPs while the run reports nothing. Every live table is flat today, so this was
    latent rather than firing; the catalog permits the depth through its ordinary door.

    The MIDDLE segments are deliberately discarded: they are tenancy, not tier and not identity.
    Nothing here needs them, and guessing which of them meant "project" is exactly the conflation
    this docstring exists to prevent.
    """
    if not object_id.startswith("table:"):
        return None
    identifier = object_id.removeprefix("table:")
    if delimiter not in identifier:
        return None
    segments = identifier.split(delimiter)
    project, table = segments[0], segments[-1]
    return (project, table) if project and table else None


#: Principals that name no PERSON. A wildcard is a statement about everyone and therefore about no
#: one; a userset addresses a group; a bare prefix addresses nothing at all. Carrying any of them
#: writes into an inbox actor literally named `*` — worse than silence, because it looks delivered.
_NOT_A_PERSON = frozenset({"", "*", "user:*"})


def _publisher(event: dict[str, Any]) -> str:
    """The verified sub of the person who published, or ``""``.

    The catalog stamps ``actor=f"user:{token.sub}"`` on every ``table_published`` control event
    (`catalog/api/v1/endpoints/publication.py`), and this head is the LAST place that identity
    exists: by the time a silver or gold stage fails, the request is gone and the mover authors as a
    chart role literal (`data_eng`), which addresses an inbox actor named `data_eng` and reaches
    nobody. Reading it here is what lets a failure two stages later still name the person.

    Only a ``user:`` principal is an address — a userset (`team:x#member`), a service, or the managed
    -access wildcard is refused rather than carried. It never authorizes anything: the notifications
    plane re-derives every recipient's visibility at delivery.
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

    parts = _split_object_id(str(data.get("object_id") or ""), DELIMITER)
    if parts is None:
        return _SUCCESS
    # TWO NAMING SYSTEMS MEET HERE, and conflating them is what the first version of this head did.
    #
    #   * the CATALOG names a table `<namespace>$<table>`, where the namespace is the TENANT
    #     (`project > warehouse > namespace`, and the ingest plane creates a namespace per project);
    #   * the MEDALLION names a lane `<tier>$<lane>` — `bronze$events` — and that name is IDENTICAL
    #     for every tenant, with the tenant travelling separately in `project`. `transform.py:109`
    #     compares the arrived name against the RAW `settings.from_dataset` and says so explicitly:
    #     "the trigger carries the unqualified name for every tenant".
    #
    # This head published the CATALOG identifier as the lane, so `acme$events` was compared against
    # `bronze$events` and every tenant's publication was DROPped as another lane's. It appeared to
    # work exactly once, in a test whose tenant happened to be NAMED `bronze` and whose table happened
    # to be named `events` — the two systems' strings collided and nothing was actually translated.
    # `ingest_trigger._bronze_write_dataset` is the reference: it returns `settings.bronze_dataset`,
    # never a per-tenant string.
    tenant, table = parts

    extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
    trigger: dict[str, Any] = {
        "token": str(data.get("event_id") or ""),
        # The LANE, tier-qualified from the medallion's own setting — the same string for every
        # tenant, which is what makes the mover's discriminator work at all.
        "dataset": f"{settings.bronze_namespace}{DELIMITER}{table}",
        "namespace": settings.bronze_namespace,
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
    # OMITTED when empty rather than sent as "": `transform.py` treats a present-but-unsafe project as
    # deterministic garbage and DROPs, so an empty string would refuse every single-tenant trigger.
    # Same conditional as the reference head.
    if tenant:
        trigger["project"] = tenant
    # THE HUMAN THE CASCADE IS FOR. Omitted when absent rather than sent blank, exactly like
    # `project` above and like the sibling head (`ingest_trigger.py`): `""` is not an identity, and a
    # present-but-empty originator would be carried all the way to an inbox actor named "".
    publisher = _publisher(data)
    if publisher:
        trigger["originator"] = publisher

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
