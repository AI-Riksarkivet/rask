"""The event lane's decision: does this lineage event mean a dataset may need maintenance?

The sweep's whole-estate walk is what this replaces as the PRIMARY trigger — measured at 87 datasets
per tick, one manifest open each, reporting `fragments_removed: 0, versions_removed: 0` on every pass
since 2026-08-16. The cron stays as an hourly BACKSTOP rather than being retired, because the bus is
provably incomplete: ingest, Ray TRAIN and every external OpenLineage producer emit over HTTP only and
never reach the topic, the catalog's lineage lane has no outbox (so a lost trigger is silent), and
time-triggered work — an old-version GC on a table nobody has written since — has no write to react to.

**The event is a hint; the plan is the decision.** `build_write_event` carries the table id, the
version and the operation, and no fragment or row count, so nothing here can tell whether there is work
— only whether it is worth asking. `dataplane.plan_compaction` answers that, and an empty task list is
already its sanctioned "nothing to do".

Subscribing to `lineage.events.v1` rather than minting a trigger topic: it is the one lane every
governed writer converges on (catalog doors, medallion movers, ingest, Ray, annotator publish), which
`notifications`' reconciler established from the other direction. The control topic is deliberately not
used — it is a broadcast with no queue group, and competing consumers need one.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel


log = logging.getLogger(__name__)

#: Catalog operations that move NO BYTES. A registration is not an arrival: `register_table` emits a
#: COMPLETE event indistinguishable — on the fields a subscriber matches — from a batch landing, and in
#: the cascade that cost one extra full run per `POST /produce` until `ingest_trigger` denied them.
#: Copied rather than imported: `services/medallion` is a sibling deployable, and a cross-service import
#: would couple two release units to share four strings.
_BYTE_FREE_OPERATIONS = frozenset({"register_table", "deregister_table", "declare_table"})

#: THE LOOP GUARD, and it must name BOTH producers or it does not close. Maintenance publishes its own
#: completion onto this same topic as `compaction` (`maintenance.core.lineage_emit.COMPACTION`) and the
#: catalog publishes `compact_table` from `/compaction_commit`
#: (`catalog.core.lineage_emit.COMPACT_TABLE`). Either one, unfiltered, is a cycle that never settles —
#: and every turn of it looks like a legitimate maintenance run on the lineage graph, so nothing would
#: report it.
_MAINTENANCE_OPERATIONS = frozenset({"compaction", "compact_table"})


class TriggeringWrite(BaseModel):
    """A write worth asking the planner about: which table, and the version it reached."""

    table_id: str
    #: The version the write produced, when the event carried one. It is the DEBOUNCE input (compare
    #: against the last-maintained stamp), never the trigger — a missing facet must not lose a real
    #: write, because the cost of a needless manifest open is one listing and the cost of a lost
    #: trigger is a table that grows fragments until the hourly backstop notices.
    version: int | None = None
    #: The physical Lance URI, from the standard ``dataSource`` facet. REQUIRED to act: this service
    #: holds no catalog client by design, so a table id alone names something it cannot open — it
    #: would have to walk the buckets to resolve it, which is the walk this lane exists to replace.
    location: str | None = None


def triggering_write(event: dict[str, Any]) -> TriggeringWrite | None:
    """The table this event says may need maintenance, or ``None`` to ignore it.

    FAILS CLOSED on anything it cannot read. Events arrive off a bus and are client-controlled, so a
    malformed one must be ignored rather than raise: a raise fails the subscription delivery, which
    Dapr redelivers, turning one bad publish into a retry loop. An event whose operation cannot be read
    is ignored for a sharper reason — an unreadable operation cannot be checked against the loop guard,
    and a cycle costs more than a trigger the hourly backstop will catch anyway.
    """
    run = event.get("run")
    facets = run.get("facets") if isinstance(run, dict) else None
    lance = facets.get("lance") if isinstance(facets, dict) else None
    operation = lance.get("operation") if isinstance(lance, dict) else None
    if not isinstance(operation, str) or operation in _BYTE_FREE_OPERATIONS or operation in _MAINTENANCE_OPERATIONS:
        return None

    outputs = event.get("outputs")
    if not isinstance(outputs, list):
        return None
    for output in outputs:
        if not isinstance(output, dict):
            continue
        table_id = output.get("name")
        if not isinstance(table_id, str) or not table_id:
            continue
        return TriggeringWrite(table_id=table_id, version=_version_of(output), location=_location_of(output))
    return None


def _version_of(output: dict[str, Any]) -> int | None:
    """The output's ``version`` facet as an int, or ``None`` when absent or unparseable.

    OpenLineage carries ``datasetVersion`` as a STRING; the caller compares it against an integer
    stamp, so the conversion happens once here rather than at each comparison site.
    """
    facets = output.get("facets")
    version_facet = facets.get("version") if isinstance(facets, dict) else None
    raw = version_facet.get("datasetVersion") if isinstance(version_facet, dict) else None
    if not isinstance(raw, str | int):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _location_of(output: dict[str, Any]) -> str | None:
    """The output's physical URI from the standard ``dataSource`` facet, or ``None`` when absent.

    Absent is a real case, not a defect to fail on: a producer outside the catalog may emit a write
    event without one, and the hourly backstop reaches those tables by discovery anyway. What this
    must never do is guess a path — a URI nobody confirmed would point maintenance at the wrong object.
    """
    facets = output.get("facets")
    source = facets.get("dataSource") if isinstance(facets, dict) else None
    uri = source.get("uri") if isinstance(source, dict) else None
    return uri if isinstance(uri, str) and uri else None


def should_replan(*, last_planned: int | None, event_version: int | None, min_versions: int) -> bool:
    """Have enough versions accumulated since this dataset was last planned to be worth planning again?

    THE DEBOUNCE, and it has to run before the plan rather than inside it. `plan_one` calls
    `sibling_base_refs`, which opens EVERY sibling dataset's manifest in the warehouse to read its
    `base_paths` — a whole-warehouse cost paid per event. Without this, a table taking a burst of writes
    drives one full sibling sweep per write. That is Lakekeeper's `min-snapshots-to-expire`, expressed
    against rask's own version stamps.

    Three cases MAINTAIN rather than skip, and each is the direction that fails loud instead of silent:

    * **No stamp** — this lane has never planned the dataset. Skipping would mean it is never maintained
      until something else writes a stamp.
    * **No version on the event** — a producer outside the catalog may stamp no version facet. There is
      nothing to compare, and dropping those would lose maintenance for every such producer; the stamp
      written after this pass debounces the next one.
    * **A threshold below one** — a misconfigured `0` must not read as "never plan", which would disable
      the lane silently. One is the smallest real threshold: every write plans.

    A REDELIVERY or an out-of-order event skips, and both matter: delivery is at-least-once so the same
    event arrives twice, and the bus does not order events, so an older version says nothing new.
    """
    if last_planned is None or event_version is None:
        return True
    return event_version - last_planned >= max(min_versions, 1)
