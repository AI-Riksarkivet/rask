"""The durable OpenLineage commit feed (``/events``), a Lance table the catalog keeps (ADR 0158).

**Governed**: each row is shown only if the caller ``can_get_metadata`` on every dataset it references, so
the feed cannot enumerate dataset names / creators / errors outside the caller's reach. Auth off →
pass-through.

The one exception is ``/events/projection``: the same feed with no per-dataset filter, gated instead on
the estate rung ``can_read_event_feed`` and cut to the fields that decide whom to notify.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request
from lance_namespace import InvalidInputError

from lineage.api.dependencies import ProvenanceDep, SettingsDep
from lineage.api.fga_deps import FilterDep, governed, is_external_source, require_event_feed_reader
from lineage.api.security import CurrentToken
from lineage.schemas import EventRecord, Events, FeedProjection, ProjectedEvent
from service_kit.lakehouse.naming import CATALOG_DELIMITER


router = APIRouter(tags=["query"])

# /events fetches a wider window than it returns so the visibility filter (which can drop rows) still
# yields up to `limit` newest *visible* events, not "the visible subset of the newest `limit`". (#22
# audit.) The wide window is only needed when FGA can actually drop rows — auth off is pass-through,
# so the fetch window collapses to `limit` there.
_EVENTS_FETCH = 2000
_EVENTS_RETURN = 500


def _column_lineage_datasets(event: dict) -> set[str]:
    """Every SOURCE dataset named inside an event's columnLineage facets — the datasets that field-to-field
    lineage discloses beyond the top-level inputs. In the raw OpenLineage payload these live at
    ``outputs[].facets.columnLineage.fields[<col>].inputFields[].name``. /events must govern on them too, or a
    caller who can see the OUTPUT would receive input datasets' column schemas they cannot read. Best-effort
    over an untrusted payload shape; ``{}`` (summary=True, no payload) yields the empty set."""
    out: set[str] = set()
    for o in event.get("outputs") or []:
        if not isinstance(o, dict):
            continue
        fields = (((o.get("facets") or {}).get("columnLineage") or {}).get("fields")) or {}
        if not isinstance(fields, dict):
            continue
        for field in fields.values():
            input_fields = (field.get("inputFields") if isinstance(field, dict) else None) or []
            for inf in input_fields:
                name = inf.get("name") if isinstance(inf, dict) else None
                if name:
                    out.add(name)
    return out


def _governed_datasets(record: EventRecord) -> set[str]:
    """Which datasets a feed row must be authorized against — EXTERNAL INPUTS EXCLUDED.

    The read-path twin of `enforce_output_authz`'s exemption, and it was missing. R23: raw is the
    external world, never a governed tier, so an external source has no `table:` object and no tuple
    that could ever be written for it. The write path already skips those inputs; the read path
    authorized them anyway, so every event naming one was hidden from EVERY caller — the lakehouse
    events board as much as the notifications reconciler.

    It could not apply the rule as written, because `consumer.py` persists `[d.name for d in
    event.inputs]` and the NAMESPACE — the whole discriminator — is dropped. The namespace is not lost
    though: the full payload is stored beside those columns, and `_column_lineage_datasets` already
    reads it for the same class of question. So the exemption is recovered from the payload when it is
    there.

    When it is NOT there — `summary=true` drops the payload at the SQL layer — the bare names are all
    that exist and governing on them is the only option. That is today's behaviour and this leaves it
    alone: the fix applies exactly where the information survives.

    OUTPUTS are never exempted, on the read path as on the write path. Writing is the direction that
    mutates the estate, and an output naming an external namespace is a producer claiming to have
    written the outside world.
    """
    outputs = set(record.outputs)
    payload = getattr(record, "event", None) or {}
    raw_inputs = payload.get("inputs") if isinstance(payload, dict) else None
    if not isinstance(raw_inputs, list) or not raw_inputs:
        return set(record.inputs) | outputs | _column_lineage_datasets(record.event)
    governed_inputs = {
        str(d.get("name"))
        for d in raw_inputs
        if isinstance(d, dict) and d.get("name") and not is_external_source(str(d.get("namespace") or ""), str(d.get("name")))
    }
    return governed_inputs | outputs | _column_lineage_datasets(record.event)


#: The top-level fields of an OpenLineage event that targeting reads: the state, when it happened, and the
#: producer (half of the id a static change is keyed by, `service_kit.openlineage.static_event_id`).
_TARGETING_FIELDS: Final = ("eventType", "eventTime", "producer")
#: The facets that name WHO: the verified author, and rask's own facet carrying the tenant, the originator
#: and the producer's run id. Every other facet describes the data or the job.
_TARGETING_FACETS: Final = ("author", "lance")
#: What names a dataset. Its facets (schema, column lineage, statistics) describe its contents.
_DATASET_FIELDS: Final = ("namespace", "name")


def _facets(bag: object) -> object:
    return {key: bag[key] for key in _TARGETING_FACETS if key in bag} if isinstance(bag, dict) else bag


def _dataset(item: object, *, with_facets: bool) -> object:
    if not isinstance(item, dict):
        return item
    named = {key: item[key] for key in _DATASET_FIELDS if key in item}
    if with_facets and "facets" in item:
        named["facets"] = _facets(item["facets"])
    return named


def targeting_view(event: dict[str, Any]) -> dict[str, Any]:
    """An event cut to what deciding WHOM TO TELL reads, and nothing describing data.

    The one consumer is the notifications reconciler, and what it reads is
    `notifications/api/lineage_events.py`'s: the run id and its `author` and `lance` facets, the state and
    time, and the output names; for a static change, the dataset's name, namespace and those two facets,
    plus `producer`. Inputs, the job, output facets (column lineage among them, which names other tables
    and their columns) and every other run facet stay in the feed and never cross this door.

    A key absent from the event stays absent and a malformed container passes through unchanged, so a row
    the reconciler would refuse whole is still refused whole: the cut narrows, it never repairs.
    """
    view = {key: event[key] for key in _TARGETING_FIELDS if key in event}
    if "run" in event:
        run = event["run"]
        view["run"] = (
            {**({"runId": run["runId"]} if "runId" in run else {}), **({"facets": _facets(run["facets"])} if "facets" in run else {})}
            if isinstance(run, dict)
            else run
        )
    if "outputs" in event:
        outputs = event["outputs"]
        view["outputs"] = [_dataset(item, with_facets=False) for item in outputs] if isinstance(outputs, list) else outputs
    if "dataset" in event:
        view["dataset"] = _dataset(event["dataset"], with_facets=True)
    return view


def _one_direction(after: int | None, since: int | None) -> None:
    """``after`` pages backward and ``since`` forward; a request naming both has no single order."""
    if after is not None and since is not None:
        raise InvalidInputError("pass `after` (page older) or `since` (follow forward), not both")


def _names_the_scope(record: EventRecord, table: str | None, namespace: str | None) -> bool:
    """True when the row's inputs or outputs name ``table``, or a table under ``namespace``.

    A dataset's name is its catalog table id joined by the catalog delimiter, so a namespace contains
    the names that start with ``<namespace><delimiter>``.
    """
    names = {*record.inputs, *record.outputs}
    if table is not None and table not in names:
        return False
    return namespace is None or any(name.startswith(f"{namespace}{CATALOG_DELIMITER}") for name in names)


def _forward_cursor(returned: list[EventRecord], records: list[EventRecord], *, limit: int, since: int, head: int) -> int:
    """The ``since`` a follower passes next: the last seq this page COVERED, never behind ``since`` within this feed.

    A full page ends at its last returned row, so the rows after it in the fetch window are read next
    time. A short page covered the whole window, rows the filters or the caller's grants dropped
    included, so the cursor moves to the window's last seq. An empty window leaves the cursor where it
    was, which is how a follower knows it has caught up.

    A ``since`` past ``head``, the newest seq the feed holds, is answered with ``head``: no cursor this feed
    issued can exceed it, so the follower holds a cursor taken on another feed (the store moved, or was
    restored), and a cursor that moves backward is how it learns that (`docs/COMMIT-EVENTS.md`).
    """
    if len(returned) == limit and returned:
        return returned[-1].seq
    return records[-1].seq if records else min(since, head)


@router.get("/events/projection")
async def get_events_projection(
    request: Request,
    provenance: ProvenanceDep,
    settings: SettingsDep,
    token: CurrentToken,
    after: Annotated[int | None, Query(ge=1)] = None,
    since: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=_EVENTS_RETURN)] = _EVENTS_RETURN,
) -> FeedProjection:
    """The run feed WITHOUT the per-dataset filter, for the notifications reconciler ([[CTL-021]]).

    WHY A SECOND DOOR RATHER THAN A FLAG ON THE FIRST. `/events` is governed per dataset, which is
    right for a person: an event naming a table you cannot see must not disclose it. It is wrong for a
    SERVICE that has to reconcile the whole estate, and the wrongness is silent in both directions:
    measured on this estate 2026-09-09, a run that demonstrably exists answered **404** to a service
    principal, and its inputs answered **200 with an empty list**. A walker sees "nothing here", and an
    estate with no work looks identical to an estate it cannot see.

    THE SERVICE IS NOT THE DISCLOSURE BOUNDARY, and that is what makes this sound rather than a hole.
    A reconciler reads the feed to decide whom to TELL; the telling is gated per subject at delivery
    (`can_be_notified`) and again at render (`can_get_metadata`), which are the checks that protect a
    person's inbox. Filtering the reconciler's own view protects nobody and only guarantees it cannot
    find the events it exists to catch.

    GATED ON `can_read_event_feed` AT THE ROOT OBJECT, a rung of its own: `event_reader`, which the
    chart grants the reconciler by name, or the estate owner. Each row carries only
    :func:`targeting_view`, because the holder reads the feed to address people and needs nothing more.

    The cursors are `/events`' own: `since` follows FORWARD, oldest first, and `next_cursor` is the
    `since` to pass next; `after` pages OLDER. `oldest_seq` is reported here too: a follower whose cursor
    falls below it lost a window to the prune, which is the one signal that distinguishes "caught up"
    from "rows went past me". There is no `summary`: the summary drops the payload, and the payload is
    the only place a run id lives.
    """
    _one_direction(after, since)
    await require_event_feed_reader(request, settings, token)
    records = await provenance.list_events(limit=limit, after=after, since=since, summary=False)
    bounds = await provenance.feed_bounds()
    if since is not None:
        next_cursor: int | None = _forward_cursor(records, records, limit=limit, since=since, head=bounds[1] if bounds else 0)
    else:
        next_cursor = records[-1].seq if len(records) == limit and records else None
    return FeedProjection(
        events=[ProjectedEvent(seq=record.seq, event=targeting_view(record.event)) for record in records],
        next_cursor=next_cursor,
        oldest_seq=bounds[0] if bounds else None,
    )


@router.get("/events")
async def get_events(
    provenance: ProvenanceDep,
    datasets: FilterDep,
    settings: SettingsDep,
    after: Annotated[int | None, Query(ge=1)] = None,
    since: Annotated[int | None, Query(ge=0)] = None,
    table: Annotated[str | None, Query(min_length=1)] = None,
    namespace: Annotated[str | None, Query(min_length=1)] = None,
    limit: Annotated[int, Query(ge=1, le=_EVENTS_RETURN)] = _EVENTS_RETURN,
    summary: bool = False,
) -> Events:
    """The ingested OpenLineage events — newest first, or FORWARD from ``since`` for a follower.

    **Following (LH-472).** ``since=<seq>`` returns the events with a larger seq, oldest first, and
    ``next_cursor`` is always the ``since`` to pass next; it equals the one passed when nothing new has
    committed. Seq order is commit order (a row's stable Lance row id, assigned when its commit lands; see
    ``lineage.services.provenance``), so a follower that always passes
    ``next_cursor`` back reads every row it may see exactly once. ``table=`` keeps the rows whose inputs or
    outputs name that table id; ``namespace=`` the rows naming a table under it. The contract a pipeline
    polls is ``docs/COMMIT-EVENTS.md``.

    **Durable** (a Lance table the catalog keeps, shared by every replica) and **governed**: when auth is
    on the feed is filtered like the per-dataset reads — an event is shown only if the caller
    ``can_get_metadata`` on *every* dataset it references (and a dataset-less event is hidden), so the
    audit feed never discloses a table outside the caller's reach. Auth off → pass-through. (#22)

    Pagination (additive, defaults = the old behavior): ``after`` = keyset cursor (the previous
    page's ``next_cursor``); ``limit`` ≤ 500 (server-capped); ``summary=true`` drops the full
    ``event`` payload from the read itself. The governance filter ALWAYS runs before the slice —
    pagination can never disclose a hidden row's CONTENT. ``next_cursor`` is a WINDOW FLOOR, not
    necessarily a visible row's seq (on a hidden-dense page it is the fetch window's last seq —
    exclusive, so the hidden row itself is never returned; bare seq numbers were already inferable
    from gaps in the pre-pagination feed, so this adds no new disclosure class — reviewed
    2026-07-11).
    """
    _one_direction(after, since)
    # Unfiltered with auth off, every fetched row is returned, so read exactly `limit` rows. A scope
    # filter or the governance filter drops rows, so keep the wide window then, or a page starves.
    scoped = table is not None or namespace is not None
    fetch = _EVENTS_FETCH if settings.fga_enabled or scoped else limit
    records = await provenance.list_events(limit=fetch, after=after, since=since, summary=summary)
    # Govern on EVERY dataset the returned payload discloses — not just top-level inputs/outputs. The full
    # raw ``event`` payload (present when summary=False) carries columnLineage facets that name SOURCE datasets
    # + column schemas NOT in the top-level inputs; without unioning them in, a caller who can see the output
    # would receive column schemas of input datasets they cannot read (audit: cross-tenant facet leak). When
    # summary=True the payload is dropped, so the extra set is empty and nothing is over-restricted.
    visible = await governed(
        datasets,
        settings.fga_enabled,
        [record for record in records if _names_the_scope(record, table, namespace)],
        _governed_datasets,
    )
    returned = visible[:limit]
    bounds = await provenance.feed_bounds()
    if since is not None:
        next_cursor: int | None = _forward_cursor(returned, records, limit=limit, since=since, head=bounds[1] if bounds else 0)
    elif len(returned) == limit and returned:
        next_cursor = returned[-1].seq  # continue right below the last VISIBLE row we returned
    elif len(records) == fetch and records:
        # The fetch window filled up but visibility filtering left a short page — more rows exist
        # below the window; hand back the window's floor so the client can keep paging.
        next_cursor = records[-1].seq
    else:
        next_cursor = None  # the feed is exhausted
    # UNGOVERNED, deliberately: a seq number names no dataset and discloses nothing the governance
    # filter above is protecting. It is precisely the number a caller needs in order to learn that a
    # page it can NEVER see went missing — withholding it would hide a data-loss signal behind a rule
    # written to hide dataset contents.
    return Events(events=returned, next_cursor=next_cursor, oldest_seq=bounds[0] if bounds else None)
