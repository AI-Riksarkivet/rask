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

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import TransactionNotFoundError, UnauthenticatedError, UnsupportedOperationError
from pydantic import ValidationError

from lineage.api.dependencies import RepositoryDep, SettingsDep
from lineage.api.fga_deps import FilterDep, enforce_output_authz, governed
from lineage.api.security import CurrentToken
from lineage.core.config import storage_options
from lineage.models import RunEvent
from lineage.schemas import DlqBacklog, DlqEvent, DlqReplayResponse
from lineage.services.consumer import record_event_best_effort
from service_kit.governed import audit
from service_kit.governed.audit import SUCCESS
from service_kit.lakehouse import outbox


log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/dlq", tags=["admin"])


def _summary(run_id: str, event_json: str) -> DlqEvent:
    """Parse a staged event into its ops summary; a poison (unparseable) object surfaces by run_id alone so
    the operator can SEE the stuck event the relay would silently drop."""
    try:
        event = RunEvent.model_validate_json(event_json)
    except ValidationError:
        return DlqEvent(run_id=run_id, parseable=False)
    return DlqEvent(
        run_id=event.run.run_id or run_id,
        event_type=event.event_type,
        event_time=event.event_time,
        job=event.job.name,
        inputs=[d.name for d in event.inputs if d.name],
        outputs=[d.name for d in event.outputs if d.name],
    )


@router.get("", response_model=DlqBacklog)
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


@router.post("/{run_id}/replay", response_model=DlqReplayResponse)
async def replay_dlq(
    run_id: str,
    request: Request,
    repository: RepositoryDep,
    settings: SettingsDep,
    token: CurrentToken,
    datasets: FilterDep,
) -> DlqReplayResponse:
    """Re-ingest one staged event on demand, then drop it — the manual twin of the reconcile relay's drain.

    A replay IS a re-ingest, so it carries the SAME authz as a fresh ingest: ``can_write_data`` on the
    event's outputs + ``can_get_metadata`` on its inputs (:func:`enforce_output_authz`, fail-closed). The
    ingest MERGEs on ``run_id`` (idempotent), the durable feed insert is ON CONFLICT DO NOTHING, and the drop
    is only reached on success — a failed re-ingest leaves the object staged for the relay to retry.

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
        event = RunEvent.model_validate_json(event_json)
    except ValidationError as exc:
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
    # Same gate as ingest — a caller may only replay a run they were authorized to write in the first place.
    await enforce_output_authz(event, request, settings, token)
    await repository.ingest_event(event)  # idempotent MERGE on run_id
    await record_event_best_effort(repository, event)  # project onto the durable /events feed too
    # Drop exactly what was replayed — dropping by run id would miss it and leave a redundant object.
    await run_in_threadpool(outbox.drop_event, settings.outbox_uri, opts, staged_key)
    # A completed ACTION records SUCCESS unconditionally (house vocabulary: ALLOW/DENY belong to authz
    # decisions — mirror access_grant/vend_credentials); the subject is empty in auth-off dev.
    audit.audit("dlq_replay", SUCCESS, subject=token.sub if token else "", resource=f"run:{run_id}")
    log.info("lineage_dlq_replayed", extra={"run_id": run_id, "sub": token.sub if token else None})
    return DlqReplayResponse(status="replayed", run_id=run_id)
