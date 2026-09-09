"""Run-status board (``/runs``) and the durable OpenLineage event feed (``/events``).

Both are **durable** (folded onto AGE / read from Postgres — survive restart, replica-shared) and
**governed**: each row is shown only if the caller ``can_get_metadata`` on every dataset it references,
so neither board can enumerate dataset names / creators / errors outside the caller's reach. Auth off →
pass-through.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from lineage.api.dependencies import RepositoryDep, SettingsDep
from lineage.api.fga_deps import FilterDep, governed, is_external_source, require_estate_observer
from lineage.api.security import CurrentToken
from lineage.schemas import Events, RunInputs, Runs, RunStatus
from lineage.services.repository import EventRecord


router = APIRouter(tags=["query"])

# /events fetches a wider window than it returns so the visibility filter (which can drop rows) still
# yields up to `limit` newest *visible* events, not "the visible subset of the newest `limit`". (#22
# audit.) The wide window is only needed when FGA can actually drop rows — auth off is pass-through,
# so the fetch window collapses to `limit` there (§2 perf, 2026-07-11: the 2s poll was reading 2000
# full-JSONB rows to return 500).
#: The runs board's page and its governance headroom — the `/events` shape, for the same reason: the
#: visibility filter runs after the read, so a page cut to size first comes back short.
_RUNS_RETURN = 200
_RUNS_FETCH = 2000
_EVENTS_FETCH = 2000
_EVENTS_RETURN = 500


def _column_lineage_datasets(event: dict) -> set[str]:
    """Every SOURCE dataset named inside an event's columnLineage facets — the datasets that field-to-field
    lineage discloses beyond the top-level inputs. In the raw OpenLineage payload these live at
    ``outputs[].facets.columnLineage.fields[<col>].inputFields[].name``. /events must govern on them too, or a
    caller who can see the OUTPUT would receive input datasets' column schemas they cannot read. Best-effort
    over untrusted JSONB shape; ``{}`` (summary=True, no payload) yields the empty set."""
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


@router.get("/runs")
async def get_runs(
    repository: RepositoryDep,
    datasets: FilterDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=_RUNS_RETURN)] = _RUNS_RETURN,
) -> Runs:
    """Live run-status board — each run's current state folded onto its ``(:Run)`` node in Apache AGE.

    **Durable** (survives restart / replica-shared) and **governed** like ``/events`` and the per-dataset
    reads: a run is shown only if the caller ``can_get_metadata`` on every dataset it wrote, so the board
    can't enumerate dataset names / creators / errors outside the caller's reach. Auth off → pass-through.

    **BOUNDED AT THE QUERY, newest first.** The board is polled every two seconds and the graph has no run
    retention, so an unbounded read grows without limit: measured live 2026-09-07 it answered 5,122 runs /
    2.65 MB, against 272 rows fifteen days earlier. A limit applied here rather than in the Cypher would
    change nothing — the cost is the READ, and asking for one run took 2.5 s while asking for a hundred
    took 1.4 s.

    The over-fetch window is ``/events``' answer, for the same reason: governance drops rows AFTER the
    read, so a page cut to size first comes back short — or empty — while visible runs sit below it. With
    auth off the filter is pass-through and the headroom is pure waste, so the fetch is exactly ``limit``.
    """
    fetch = _RUNS_FETCH if settings.fga_enabled else limit
    result = await repository.list_runs(limit=fetch)
    visible = await governed(datasets, settings.fga_enabled, result.runs, lambda r: set(r.outputs))
    result.runs = visible[:limit]
    return result


@router.get("/runs/{run_id}")
async def get_run(run_id: str, repository: RepositoryDep, datasets: FilterDep, settings: SettingsDep) -> RunStatus:
    """ONE run's state — the point read the board is not.

    "Is this run in the graph?" was answered by downloading `/runs` and scanning it, which is
    O(estate) for a one-row question and became WRONG once the board was bounded: a run outside the
    newest page is missing from the response while present in the graph, so the caller concluded
    ABSENT and reported a provenance defect that did not exist.

    GOVERNED EXACTLY LIKE THE BOARD — visible only if the caller `can_get_metadata` on every dataset
    the run wrote — and an invisible run answers 404, the same as one that is not there. Deliberately
    the same answer: distinguishing them would let a caller enumerate runs it may not see by watching
    which ids give a different refusal.
    """
    run = await repository.run_status(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    visible = await governed(datasets, settings.fga_enabled, [run], lambda r: set(r.outputs))
    if not visible:
        raise HTTPException(status_code=404, detail="run not found")
    return visible[0]


@router.get("/runs/{run_id}/inputs")
async def get_run_inputs(run_id: str, repository: RepositoryDep, datasets: FilterDep, settings: SettingsDep) -> RunInputs:
    """The inputs a run consumed, each with the version it PINNED — the per-run reproducibility answer.

    The pinned version rides the graph's ``READ`` edge (the Ray TRAIN job pins every feature — #115
    D1); this endpoint is its API surface (before, Cypher-only). Kept OFF the 2s-polled ``/runs`` board
    — it's a per-run drill-in, and adding a per-run READ-edge fetch to the board would be N+1 on the hot
    path. Governed like every read: an input the caller can't ``can_get_metadata`` is dropped (an empty
    list for a run the caller can't see into is itself non-disclosing). Auth off → pass-through.
    """
    result = await repository.run_inputs(run_id)
    result.inputs = await governed(datasets, settings.fga_enabled, result.inputs, lambda i: {i.name})
    return result


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
        str(d.get("name")) for d in raw_inputs if isinstance(d, dict) and d.get("name") and not is_external_source(str(d.get("namespace") or ""))
    }
    return governed_inputs | outputs | _column_lineage_datasets(record.event)


@router.get("/events/projection")
async def get_events_projection(
    request: Request,
    repository: RepositoryDep,
    settings: SettingsDep,
    token: CurrentToken,
    after: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=_EVENTS_RETURN)] = _EVENTS_RETURN,
    summary: bool = False,
) -> Events:
    """The feed WITHOUT the per-dataset filter, for a caller that observes the estate (§ G1).

    WHY A SECOND DOOR RATHER THAN A FLAG ON THE FIRST. `/events` is governed per dataset, which is
    right for a person: an event naming a table you cannot see must not disclose it. It is wrong for a
    SERVICE that has to reconcile the whole estate, and the wrongness is silent in both directions —
    measured on this estate 2026-09-09, a run that demonstrably exists answered **404** to a service
    principal, and its inputs answered **200 with an empty list**. A walker sees "nothing here", and an
    estate with no work looks identical to an estate it cannot see.

    THE SERVICE IS NOT THE DISCLOSURE BOUNDARY, and that is what makes this sound rather than a hole.
    A reconciler reads the feed to decide who to TELL; the telling is gated per subject at delivery
    (`can_be_notified`), which is the check that actually protects a person's inbox. Filtering the
    reconciler's own view protects nobody and only guarantees it cannot find the events it exists to
    catch. `can_be_notified` stays the sole disclosure gate; this door moves the estate-read decision
    to the rung that means "may observe the estate".

    `can_observe_events` ON THE ROOT OBJECT — the same rung `POST /v1/projects` and `POST /v1/stores`
    already gate on, so an estate privilege means one thing everywhere. It is `owner` on the root in
    `model.fga`, so nobody holds it by accident and granting it is a deliberate act.

    Identical shape to `/events` otherwise — same keyset cursor, same cap, same `summary` — so a caller
    can move between the two without a second client. `oldest_seq` is reported here too: a walker whose
    cursor falls below it lost a window to the prune, which is the one signal that distinguishes
    "caught up" from "rows went past me".
    """
    await require_estate_observer(request, settings, token)
    records = await repository.list_events(limit=limit, after=after, summary=summary)
    next_cursor = records[-1].seq if len(records) == limit and records else None
    return Events(events=records, next_cursor=next_cursor, oldest_seq=await repository.oldest_event_seq())


@router.get("/events")
async def get_events(
    repository: RepositoryDep,
    datasets: FilterDep,
    settings: SettingsDep,
    after: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=_EVENTS_RETURN)] = _EVENTS_RETURN,
    summary: bool = False,
) -> Events:
    """The most-recent ingested OpenLineage events (newest first) — the Marquez-style event feed.

    **Durable** (read from Postgres, survives restart / replica-shared) and **governed**: when auth is
    on the feed is filtered like the per-dataset reads — an event is shown only if the caller
    ``can_get_metadata`` on *every* dataset it references (and a dataset-less event is hidden), so the
    audit feed never discloses a table outside the caller's reach. Auth off → pass-through. (#22)

    Pagination (additive, defaults = the old behavior): ``after`` = keyset cursor (the previous
    page's ``next_cursor``); ``limit`` ≤ 500 (server-capped); ``summary=true`` drops the full-JSONB
    ``event`` payload at the SQL layer. The governance filter ALWAYS runs before the slice —
    pagination can never disclose a hidden row's CONTENT. ``next_cursor`` is a WINDOW FLOOR, not
    necessarily a visible row's seq (on a hidden-dense page it is the fetch window's last seq —
    exclusive, so the hidden row itself is never returned; bare seq numbers were already inferable
    from gaps in the pre-pagination feed, so this adds no new disclosure class — reviewed
    2026-07-11).
    """
    # Auth off → governed() is pass-through, so no over-fetch headroom is needed: read exactly
    # `limit` rows. Auth on → keep the wide window so dropped rows don't starve the page.
    fetch = _EVENTS_FETCH if settings.fga_enabled else limit
    records = await repository.list_events(limit=fetch, after=after, summary=summary)
    # Govern on EVERY dataset the returned payload discloses — not just top-level inputs/outputs. The full
    # raw ``event`` JSONB (present when summary=False) carries columnLineage facets that name SOURCE datasets
    # + column schemas NOT in the top-level inputs; without unioning them in, a caller who can see the output
    # would receive column schemas of input datasets they cannot read (audit: cross-tenant facet leak). When
    # summary=True the payload is dropped, so the extra set is empty and nothing is over-restricted.
    visible = await governed(
        datasets,
        settings.fga_enabled,
        records,
        _governed_datasets,
    )
    returned = visible[:limit]
    if len(returned) == limit and returned:
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
    return Events(events=returned, next_cursor=next_cursor, oldest_seq=await repository.oldest_event_seq())
