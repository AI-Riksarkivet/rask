"""Admin DLQ / transactional-outbox ops endpoints (#83) — view + replay the at-risk lineage events.

The lineage delivery path has two failure backstops (see ``docs/RESILIENCE.md``): the **park**
(`lineage.api.ingest_lane.park`: a delivery the ingest lane ends unrecorded, or a staged event the relay drains that a
grant would admit, is kept in the provenance namespace's ``refusals`` table and replayed at this module's ``/parked``
doors), and the **transactional outbox** (``lakehouse.events.outbox``) — an object-store of events staged BEFORE publish and
dropped on ack. A surviving outbox object is a committed write whose lineage was not yet confirmed delivered:
the at-risk set the reconcile relay drains on a timer. This module surfaces that set for an operator and lets
them replay one on demand instead of waiting for the next reconcile tick.

Authz mirrors ingest exactly, because a replay IS a re-ingest: the view is filtered per-dataset (you see only
events whose datasets you may read — :func:`governed`), and a replay requires ``can_write_data`` on the
event's outputs + ``can_read_data`` on its inputs (:func:`enforce_output_authz`). Both are no-ops when FGA
is off (dev/tests). Every replay is audited on the ``lance.audit`` trail (#41).

A STAGED EVENT IS SCOPED AS THE CALLER, NOW ([[LH-598]]). No door has admitted it yet, so nothing recorded which
tables its locations and other identifiers name: the catalog is asked for every staged event the door reads
(`fga_deps.DatasetFilter.scope_unrecorded`), and an event naming a table the caller may not see under any identifier is
neither listed nor replayable, answered as an event nothing staged. An event the catalog attests by the signature its
staged bytes carry (`fga_deps.DatasetFilter.attestation`) names its tables by uuid and has none of its other
identifiers asked about: a rename's previous name, which no table holds once the rename landed. That read counts and
warns about nothing and answers an outage as "not attested", so the event takes the full check and the page still
answers; only the replay's own verification (`enforce_bus_authz`) answers a refusal or an outage as such.
"""

import base64
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import InvalidInputError, ServiceUnavailableError, TransactionNotFoundError, UnsupportedOperationError

from lakehouse.events import outbox
from lakehouse.events.bus import BusUnavailableError
from lakehouse.events.bus_delivery import Verdict
from lakehouse.events.cloud_events import CloudEvent
from lakehouse.events.lineage_events import dataset_uuids
from lakehouse.events.outbox import StagedPosition
from lakehouse.governed import audit, fga_ids
from lakehouse.governed.audit import AuditAction, AuditOutcome
from lakehouse.governed.door_caller import caller_subject
from lineage.api.dependencies import ProvenanceDep, PublisherDep, SettingsDep
from lineage.api.fga_deps import (
    FilterDep,
    Governed,
    enforce_bus_authz,
    enforce_output_authz,
    require_caller,
    require_parks_reader,
    require_parks_replayer,
)
from lineage.api.security import CurrentToken
from lineage.core.config import LineageSettings, storage_options
from lineage.core.metrics import Door, Outcome, record_outcome
from lineage.models import DatasetEvent, RunEvent
from lineage.schemas import DlqBacklog, DlqEvent, DlqReplayResponse, ParkedRuns
from lineage.services.consumer import handle_cloud_event
from lineage.services.staged import UnparseableEventError, parse_staged, read_staged, republish_staged


log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/dlq", tags=["admin"])


def _summary(run_id: str, event_json: str) -> DlqEvent:
    """Parse a staged event into its ops summary; a poison (unparseable) object surfaces by run_id alone so
    the operator can SEE the stuck event the relay would silently drop. A static change has no run, type or
    job, so it lists under its staged key with those left empty."""
    try:
        event = parse_staged(read_staged(event_json))
    except UnparseableEventError:
        return DlqEvent(run_id=run_id, parseable=False)
    outputs = [d.name for d in event.outputs if d.name]
    if isinstance(event, DatasetEvent):
        return DlqEvent(run_id=run_id, event_time=event.event_time, outputs=outputs)
    return DlqEvent(
        run_id=event.run.run_id or run_id,
        event_type=event.event_type,
        event_time=event.event_time,
        job=event.job.name,
        inputs=[d.name for d in event.inputs if d.name],
        outputs=outputs,
    )


def _audited(event: RunEvent | DatasetEvent, key: str) -> str:
    """What a replay acted on, for the audit record: the run, or the table a static change names (it has no run)."""
    if isinstance(event, DatasetEvent):
        uuid = dataset_uuids(event.dataset.model_dump(by_alias=True)).table_uuid
        return fga_ids.table_object(uuid) if uuid else fga_ids.addressed_object("table", event.dataset.name)
    return f"run:{event.run_id or key}"


def _position(cursor: str | None) -> StagedPosition | None:
    """Where the page ``cursor`` names starts, a ``next_cursor`` this door answered; the start of the outbox for none.

    Raises:
        InvalidInputError: ``cursor`` is no cursor this door issued.
    """
    if cursor is None:
        return None
    try:
        return StagedPosition.model_validate_json(base64.urlsafe_b64decode(cursor))
    except ValueError as exc:  # `binascii.Error` and pydantic's `ValidationError` are both `ValueError`s
        raise InvalidInputError("`after` is not a cursor this door issued") from exc


def _cursor(position: StagedPosition | None) -> str | None:
    """``position`` as the opaque ``next_cursor`` a caller passes back as ``after``; ``None`` at the end of the outbox."""
    return base64.urlsafe_b64encode(position.model_dump_json().encode()).decode() if position is not None else None


@router.get("", dependencies=[Depends(require_caller)])
async def list_dlq(
    *,
    settings: SettingsDep,
    datasets: FilterDep,
    after: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> DlqBacklog:
    """The outbox saturation snapshot, and one page of the at-risk events the caller may see, oldest first.

    ``depth``/``oldest_age_seconds`` are the raw system-health signal, served to any authenticated caller when FGA is
    on (:func:`require_caller`, else the raw depth would leak to an anonymous one); ``events`` is the per-dataset-governed
    subset of this page the caller may see.

    Paged as the feed is (``/events``): a request covers the next ``limit`` staged events after ``after``, whichever of
    them the caller may see, and ``next_cursor`` is the ``after`` to pass next, ``null`` once the page reached the end of
    the outbox. A page can therefore be empty, or short, with more to read: a caller whose events sit behind others it
    may not see reaches them by following the cursor, at ``limit`` events' work a request.

    A caller that follows the cursor is shown every event it may see that stayed staged throughout its walk exactly
    once, whatever is delivered or staged meanwhile; an event staged again during the walk can be shown again. The
    cursor is opaque. It holds the time the last event the page covered was staged at and a digest of that event's id,
    never the id: a caller that already knows an event's id could tell from it that the event was staged then, and
    learns nothing else of an event it may not see.
    """
    if not settings.outbox_uri:
        # No outbox configured (the durable backstop is opt-in) — an honest empty snapshot, not a 500.
        return DlqBacklog(depth=0, oldest_age_seconds=0.0, events=[], limit=limit)
    opts = storage_options(settings)
    position = _position(after)
    depth, oldest_age = await run_in_threadpool(outbox.backlog, settings.outbox_uri, opts)
    page = await run_in_threadpool(lambda: outbox.page_events(settings.outbox_uri, opts, limit=limit, after=position))
    staged = page.events
    # Drop events referencing datasets the caller may not see (fail-closed). A poison event has no parseable
    # datasets, so under FGA it is filtered out (dataset-less → dropped); in dev/tests (FGA off) it shows.
    parsed = [_parsed(event_json) for _, event_json in staged]
    readable = [held for held in parsed if held is not None]
    # Only scoping reads a signature, and with FGA off nothing is scoped.
    signatures = [await datasets.attestation(arrived) if settings.fga_enabled else None for _, arrived in readable]
    scopes = iter(await datasets.scope_unrecorded([event for event, _ in readable], signatures))
    # A poison object names nothing, so nothing is asked about it.
    events = [
        (_summary(run_id, event_json), next(scopes) if event is not None else Governed()) for (run_id, event_json), event in zip(staged, parsed, strict=True)
    ]
    visible = await datasets.governed(events, refs=lambda pair: pair[1])
    return DlqBacklog(depth=depth, oldest_age_seconds=oldest_age, events=[summary for summary, _ in visible], limit=limit, next_cursor=_cursor(page.next))


def _parsed(event_json: str) -> tuple[RunEvent | DatasetEvent, dict[str, Any]] | None:
    """The event a staged object holds and the bytes its producer signed, or ``None`` for a poison object."""
    try:
        staged = read_staged(event_json)
        return parse_staged(staged), staged.data
    except UnparseableEventError:
        return None


@router.post("/{run_id}/replay")
async def replay_dlq(
    *,
    run_id: str,
    request: Request,
    provenance: ProvenanceDep,
    settings: SettingsDep,
    token: CurrentToken,
    datasets: FilterDep,
    publisher: PublisherDep,
) -> DlqReplayResponse:
    """Re-ingest one staged event on demand, re-publish it, then drop it — the manual twin of the reconcile relay's drain.

    A replay IS a re-ingest, so it carries the SAME authz as a fresh ingest, twice over: the relay's gate on
    the staged bytes (:func:`enforce_bus_authz` — signature and stamped author) and the ingest door's on the
    operator (:func:`enforce_output_authz` — ``can_write_data`` on the outputs, ``can_read_data`` on the
    inputs, fail-closed). The record is idempotent: every feed row and edge is written under its key. The
    staged bytes are re-published
    to the lineage topic exactly as the relay re-publishes them, so subscribers hear of the event as well as
    the feed. The drop is only reached on success — a failed re-ingest or re-publish leaves the object
    staged for the relay to retry, and a re-publish the bus did not take answers 503 because the feed is already
    repaired.

    Non-disclosure (audit 2026-07-20): replay must not become an oracle for events ``list_dlq`` hid. So a
    run whose datasets the caller cannot SEE (or an unparseable poison object, which the governed list also
    hides) returns the SAME 404 as a missing run — never a distinct 422 or a 403 echoing hidden dataset
    names. Only an event the caller could have listed reaches ``enforce_output_authz``.
    """
    opts = storage_options(settings)
    staged_key, staged, event = await _staged_for_replay(settings, run_id)
    # THE LIST'S OWN RULE DECIDES (`DatasetFilter.governed`): a caller replays only an event the list would show it. One the
    # list hides answers as a missing run does: an event naming a table the caller may not read (by its name, its location,
    # another identifier or a column upstream), so `enforce_output_authz`'s 403 never names a table the list withheld, and
    # an event naming no table at all, which nothing below would ask a rung for.
    signature = await datasets.attestation(staged.data) if settings.fga_enabled else None
    if not await datasets.governed(await datasets.scope_unrecorded([event], [signature]), refs=lambda named: named):
        raise TransactionNotFoundError(f"no staged lineage event for run {run_id}")
    # TWO GATES, because a replay answers two questions. The relay's own, over the STAGED BYTES: the
    # signature and what the stamped author may record — so a replay admits nothing the relay would refuse,
    # and a forged or re-targeted object stays refused however trusted the operator is. Then the ingest
    # door's, over the operator: a caller may only replay what they could have written in the first place.
    arrived = staged.data
    # The operator's rungs are asked whoever signed; what the catalog attests is only the names its event uses.
    aliased = await enforce_bus_authz(event, request.app, settings, arrived=arrived) | await enforce_output_authz(
        event, request.app, settings, token=token, names_vouched_by=signature
    )
    await provenance.record(event, aliased=aliased)  # idempotent: every row under its key
    # BEFORE the drop: the staged object is the only copy the relay could still re-publish. The feed is
    # already repaired when this fails, so the partial outcome is audited whatever failed. A bus that did not take the
    # copy (`publish_event`: no acknowledgement in time, no connection, or a refusal) answers a 503 saying so, which the
    # operator can retry since the record is idempotent. Anything else answers as itself: an envelope past the bus's
    # payload cap is refused the same on every retry, and what the publish does not document is a fault of this service.
    try:
        await republish_staged(publisher, settings, staged)
    except (TimeoutError, BusUnavailableError) as exc:
        audit.audit(AuditAction.DLQ_REPLAY, AuditOutcome.FAILURE, subject=caller_subject(token), resource=_audited(event, run_id))
        log.warning("lineage_dlq_replay_unannounced", extra={"run_id": run_id, "error": str(exc)})
        raise ServiceUnavailableError(f"replayed {run_id} into the feed, but re-announcing it failed; it stays staged for the relay to retry") from exc
    except Exception:
        audit.audit(AuditAction.DLQ_REPLAY, AuditOutcome.FAILURE, subject=caller_subject(token), resource=_audited(event, run_id))
        raise
    # Drop exactly what was replayed — dropping by run id would miss it and leave a redundant object.
    await run_in_threadpool(outbox.drop_event, settings.outbox_uri, opts, staged_key)
    # A completed ACTION records SUCCESS unconditionally (house vocabulary: ALLOW/DENY belong to authz
    # decisions — mirror access_grant/vend_credentials); the subject is empty in auth-off dev.
    audit.audit(AuditAction.DLQ_REPLAY, AuditOutcome.SUCCESS, subject=caller_subject(token), resource=_audited(event, run_id))
    log.info("lineage_dlq_replayed", extra={"run_id": run_id})
    return DlqReplayResponse(status="replayed", run_id=run_id)


async def _staged_for_replay(settings: LineageSettings, run_id: str) -> tuple[str, CloudEvent, RunEvent | DatasetEvent]:
    """The staged object a replay of ``run_id`` addresses: its key, the envelope it holds and the event that carries.

    Raises:
        TransactionNotFoundError: Nothing is staged for the run, or, with authorization on, what is staged is a poison
            object: the governed list hides one (it names no dataset), so the replay answers it as a missing run, with
            no audited existence signal.
        UnsupportedOperationError: With authorization off, where everything is visible, what is staged is poison.
    """
    if not settings.outbox_uri:
        raise TransactionNotFoundError(f"no staged lineage event for run {run_id}")
    # Resolve the RUN to its staged object: the key is per event, and this route addresses a run.
    resolved = await run_in_threadpool(outbox.resolve_event, settings.outbox_uri, storage_options(settings), run_id)
    if resolved is None:
        raise TransactionNotFoundError(f"no staged lineage event for run {run_id}")
    staged_key, event_json = resolved
    try:
        staged = read_staged(event_json)
        return staged_key, staged, parse_staged(staged)
    except UnparseableEventError as exc:
        if settings.fga_enabled:
            raise TransactionNotFoundError(f"no staged lineage event for run {run_id}") from exc
        raise UnsupportedOperationError(f"staged event for run {run_id} is unparseable (poison)") from exc


@router.get("/parked", dependencies=[Depends(require_parks_reader)])
async def list_parked(provenance: ProvenanceDep) -> ParkedRuns:
    """The runs whose events lineage's park keeps, from the bus or the outbox, each with how many, for the estate admin ([[LH-148]])."""
    return ParkedRuns(runs=await provenance.parked_runs())


@router.post("/parked/{run_id}/replay", dependencies=[Depends(require_parks_replayer)])
async def replay_parked(*, run_id: str, request: Request, provenance: ProvenanceDep, settings: SettingsDep, token: CurrentToken) -> DlqReplayResponse:
    """Re-present the events of ``run_id`` lineage's park kept, from the bus or the outbox, once their cause is fixed ([[LH-148]]).

    Each kept CloudEvent goes through the ingest lane's own handling (``handle_cloud_event`` under
    :func:`enforce_bus_authz`: the signature over the bytes that arrived, and what the stamped author may record), so
    this admits nothing the bus refuses, however trusted the operator is. It writes to the feed directly and publishes
    nothing: the feed is what was missing the event, and a subscriber hears of it only from its producer's publish. Each delivery is
    judged on its own feed row: one the feed then holds is RECOVERED and no longer kept, and one it still lacks stays
    kept, so a run whose START landed while its COMPLETE is still refused keeps the COMPLETE.

    The estate admin's door (:func:`require_parks_replayer`, asked before any kept row is read, so it names no run to
    anyone else).

    Raises:
        TransactionNotFoundError: No delivery of the run is kept.
        InvalidInputError: A delivery is still refused on re-presentation; it stays kept, and those recovered with it
            are no longer kept.
        ServiceUnavailableError: A re-presentation met an outage (an authorization or key-store answer it could not
            read); what it could not judge stays kept.
    """
    kept = await provenance.parked(run_id)
    if not kept:
        raise TransactionNotFoundError(f"no parked lineage delivery is kept for run {run_id}")
    answers = [
        await handle_cloud_event(
            provenance, delivery.cloud_event, lambda parsed, arrived: enforce_bus_authz(parsed, request.app, settings, arrived=arrived), door=Door.DEAD_LETTER
        )
        for delivery in kept
    ]
    held = await provenance.events_recorded({delivery.feed_key for delivery in kept if delivery.feed_key})
    recovered = [delivery for delivery in kept if delivery.feed_key in held]
    if recovered:
        await provenance.forget_parked([delivery.key for delivery in recovered])
        for _ in recovered:
            record_outcome(Outcome.RECOVERED, door=Door.DEAD_LETTER)
    subject = caller_subject(token)
    left = [(delivery, answer) for delivery, answer in zip(kept, answers, strict=True) if delivery.feed_key not in held]
    if left:
        audit.audit(AuditAction.DLQ_PARKED_REPLAY, AuditOutcome.FAILURE, subject=subject, resource=f"run:{run_id}")
        names = [f"{delivery.feed_key or delivery.key}: {answer.value}" for delivery, answer in left]
        if any(answer is Verdict.AGAIN for _, answer in left):
            raise ServiceUnavailableError(f"re-presenting run {run_id} met an outage; {len(left)} delivery(ies) stay kept: {names}")
        raise InvalidInputError(f"run {run_id} is still refused on re-presentation; {len(left)} delivery(ies) stay kept, {len(recovered)} recovered: {names}")
    audit.audit(AuditAction.DLQ_PARKED_REPLAY, AuditOutcome.SUCCESS, subject=subject, resource=f"run:{run_id}")
    log.info("lineage_parked_replayed", extra={"run_id": run_id, "deliveries": len(kept)})
    return DlqReplayResponse(status="recovered", run_id=run_id)
