"""Admin DLQ / transactional-outbox ops endpoints (#83) — view + replay the at-risk lineage events.

The lineage delivery path has two failure backstops (see ``docs/RESILIENCE.md``): the Dapr-native DLQ on a
NATS JetStream stream (park-and-alert, replayed from the stream on restart — not app-queryable, by design),
and the **transactional outbox** (``service_kit.lakehouse.outbox``) — an object-store of events staged BEFORE publish and
dropped on ack. A surviving outbox object is a committed write whose lineage was not yet confirmed delivered:
the at-risk set the reconcile relay drains on a timer. This module surfaces that set for an operator and lets
them replay one on demand instead of waiting for the next reconcile tick.

Authz mirrors ingest exactly, because a replay IS a re-ingest: the view is filtered per-dataset (you see only
events whose datasets you may read — :func:`governed`), and a replay requires ``can_write_data`` on the
event's outputs + ``can_get_metadata`` on its inputs (:func:`enforce_output_authz`). Both are no-ops when FGA
is off (dev/tests). Every replay is audited on the ``lance.audit`` trail (#41).
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ServiceUnavailableError, TransactionNotFoundError, UnauthenticatedError, UnsupportedOperationError

from lineage.api.dependencies import PublisherDep, RepositoryDep, SettingsDep
from lineage.api.fga_deps import FilterDep, enforce_bus_authz, enforce_output_authz, governed
from lineage.api.security import CurrentToken
from lineage.core.config import storage_options
from lineage.models import DatasetEvent, RunEvent
from lineage.schemas import DlqBacklog, DlqEvent, DlqReplayResponse
from lineage.services.staged import UnparseableEventError, parse_staged, republish_staged
from service_kit.governed import audit
from service_kit.governed.audit import FAILURE, SUCCESS
from service_kit.lakehouse import outbox


log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/dlq", tags=["admin"])


def _summary(run_id: str, event_json: str) -> DlqEvent:
    """Parse a staged event into its ops summary; a poison (unparseable) object surfaces by run_id alone so
    the operator can SEE the stuck event the relay would silently drop. A static change has no run, type or
    job, so it lists under its staged key with those left empty."""
    try:
        event = parse_staged(event_json)
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
    return f"table:{event.dataset.name}" if isinstance(event, DatasetEvent) else f"run:{event.run_id or key}"


@router.get("")
async def list_dlq(
    settings: SettingsDep,
    token: CurrentToken,
    datasets: FilterDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> DlqBacklog:
    """The outbox saturation snapshot + the visible at-risk events (oldest first, capped at ``limit``).

    ``depth``/``oldest_age_seconds`` are the raw system-health signal; ``events`` is the per-dataset-governed
    subset the caller may see. Requires an authenticated principal when FGA is on (else the raw depth would
    leak to an anonymous caller); the event list is then filtered by dataset visibility.
    """
    if settings.fga_enabled and token is None:
        raise UnauthenticatedError("authentication required")
    if not settings.outbox_uri:
        # No outbox configured (the durable backstop is opt-in) — an honest empty snapshot, not a 500.
        return DlqBacklog(depth=0, oldest_age_seconds=0.0, events=[], limit=limit)
    opts = storage_options(settings)
    depth, oldest_age = await run_in_threadpool(outbox.backlog, settings.outbox_uri, opts)
    staged = await run_in_threadpool(lambda: list(outbox.list_events(settings.outbox_uri, opts, limit=limit)))
    events = [_summary(run_id, event_json) for run_id, event_json in staged]
    # Drop events referencing datasets the caller may not see (fail-closed). A poison event has no parseable
    # datasets, so under FGA it is filtered out (dataset-less → dropped); in dev/tests (FGA off) it shows.
    visible = await governed(datasets, settings.fga_enabled, events, lambda e: set(e.outputs) | set(e.inputs))
    return DlqBacklog(depth=depth, oldest_age_seconds=oldest_age, events=visible, limit=limit)


@router.post("/{run_id}/replay")
async def replay_dlq(
    run_id: str,
    request: Request,
    repository: RepositoryDep,
    settings: SettingsDep,
    token: CurrentToken,
    datasets: FilterDep,
    publisher: PublisherDep,
) -> DlqReplayResponse:
    """Re-ingest one staged event on demand, re-publish it, then drop it — the manual twin of the reconcile relay's drain.

    A replay IS a re-ingest, so it carries the SAME authz as a fresh ingest, twice over: the relay's gate on
    the staged bytes (:func:`enforce_bus_authz` — signature and stamped author) and the ingest door's on the
    operator (:func:`enforce_output_authz` — ``can_write_data`` on the outputs, ``can_get_metadata`` on the
    inputs, fail-closed). Both doors are idempotent: a run MERGEs on its run id, a static change on its
    derived feed id, and the durable feed insert is ON CONFLICT DO NOTHING. The staged bytes are re-published
    to the lineage topic exactly as the relay re-publishes them, so subscribers hear of the event as well as
    the graph. The drop is only reached on success — a failed re-ingest or re-publish leaves the object
    staged for the relay to retry, and a failed re-publish answers 503 because the graph is already
    repaired.

    Non-disclosure (audit 2026-07-20): replay must not become an oracle for events ``list_dlq`` hid. So a
    run whose datasets the caller cannot SEE (or an unparseable poison object, which the governed list also
    hides) returns the SAME 404 as a missing run — never a distinct 422 or a 403 echoing hidden dataset
    names. Only an event the caller could have listed reaches ``enforce_output_authz``.
    """
    if not settings.outbox_uri:
        raise TransactionNotFoundError(f"no staged lineage event for run {run_id}")
    opts = storage_options(settings)
    # Resolve the RUN to its staged object: the key is per-event now, and this route addresses a run.
    resolved = await run_in_threadpool(outbox.resolve_event, settings.outbox_uri, opts, run_id)
    if resolved is None:
        raise TransactionNotFoundError(f"no staged lineage event for run {run_id}")
    staged_key, event_json = resolved
    try:
        event = parse_staged(event_json)
    except UnparseableEventError as exc:
        # A poison object is dataset-less, so the governed list_dlq already hides it under FGA — replay must
        # too: 404 (indistinguishable from missing), no audited existence signal. Auth-off dev keeps the
        # honest 422 (no disclosure concern when everything is visible).
        if settings.fga_enabled:
            raise TransactionNotFoundError(f"no staged lineage event for run {run_id}") from exc
        raise UnsupportedOperationError(f"staged event for run {run_id} is unparseable (poison)") from exc
    # Visibility gate — mirror list_dlq's governance: you may only replay an event you could have SEEN. An
    # event referencing a dataset the caller can't read is hidden as a 404, so enforce_output_authz's
    # dataset-naming 403 can never disclose a dataset the list view withheld.
    refs = {d.name for d in [*event.outputs, *event.inputs] if d.name}
    if settings.fga_enabled and refs:
        visible = await datasets.visible(list(refs))
        if refs - visible:
            raise TransactionNotFoundError(f"no staged lineage event for run {run_id}")
    # TWO GATES, because a replay answers two questions. The relay's own, over the STAGED BYTES: the
    # signature and what the stamped author may record — so a replay admits nothing the relay would refuse,
    # and a forged or re-targeted object stays refused however trusted the operator is. Then the ingest
    # door's, over the operator: a caller may only replay what they could have written in the first place.
    await enforce_bus_authz(event, request, settings, json.loads(event_json))
    await enforce_output_authz(event, request, settings, token)
    # The door the event's shape names, exactly as the bus and the relay route it.
    if isinstance(event, DatasetEvent):
        await repository.ingest_dataset_event(event)
    else:
        await repository.ingest_event(event)  # idempotent — MERGE on run_id, and the /events row in the same transaction
    # BEFORE the drop: the staged object is the only copy the relay could still re-publish. The graph is
    # already repaired when this fails, so the answer says so, and the partial outcome is audited: a 503
    # the operator can retry (the ingest is idempotent) rather than an unexplained 500 with no record.
    try:
        await republish_staged(publisher, settings, event_json)
    except Exception as exc:
        audit.audit("dlq_replay", FAILURE, subject=token.sub if token else "", resource=_audited(event, run_id))
        log.warning("lineage_dlq_replay_unannounced", extra={"run_id": run_id, "error": str(exc)})
        raise ServiceUnavailableError(f"replayed {run_id} into the graph, but re-announcing it failed; it stays staged for the relay to retry") from exc
    # Drop exactly what was replayed — dropping by run id would miss it and leave a redundant object.
    await run_in_threadpool(outbox.drop_event, settings.outbox_uri, opts, staged_key)
    # A completed ACTION records SUCCESS unconditionally (house vocabulary: ALLOW/DENY belong to authz
    # decisions — mirror access_grant/vend_credentials); the subject is empty in auth-off dev.
    audit.audit("dlq_replay", SUCCESS, subject=token.sub if token else "", resource=_audited(event, run_id))
    log.info("lineage_dlq_replayed", extra={"run_id": run_id, "sub": token.sub if token else None})
    return DlqReplayResponse(status="replayed", run_id=run_id)
