"""The incremental-ingest tick — a Dapr cron binding that re-runs a configured source (TRIGGER-2).

IT IS A POLL, said out loud because the design asked for exactly that sentence: incremental ingest is
event-driven from bronze INWARD — `enumerate_chunks` anti-joins against bronze itself to learn what is
new — and a SCHEDULED POLL at the outer boundary, where nothing tells us the source changed. Bucket
notification was rejected as the general mechanism: it covers one of three registered kinds, and IIIF
has no notification channel and never will. It remains reasonable as a later per-kind fast path for
`s3-prefix`, and wrong as the thing the design rests on.

The schedule lives in the chart as component config, never here — there is no scheduler thread in this
service, and a cadence in code cannot be changed without a deploy.

ONE PROJECT, BY CONSTRUCTION, and this is a designed limit rather than an oversight. A tick carries no
user, so the run authorizes on the service-token branch, which `auth.py` pins to
``RASK_INGEST_SERVICE_PROJECT``. A multi-tenant watch set cannot work through that door as it stands,
because nothing in the plane carries a watch creator's authority forward to fire time. Anything
broader needs that question answered first, not a wider loop here.

SAFE ON A CLOCK ONLY BECAUSE THE CEILING EXISTS. The anti-join reads every existing id to learn what
bronze already holds — O(existing rows) per tick, not O(new rows) — so before
``RASK_INGEST_INCREMENTAL_MAX_ROWS`` this trigger would have turned a per-request cost into a
recurring one. That ordering is why the trigger lands after the ceiling rather than with the mechanism.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request

from service_kit.governed.dapr_auth import require_dapr_token


logger = logging.getLogger(__name__)


def cron_request() -> dict[str, Any] | None:
    """The run this tick starts, from the deployment's config — or ``None`` when it names no source.

    Deliberately the same shape a manual ``POST /v1/ingests`` carries: the tick is not a second way to
    describe a run, it is the same run on a clock. `project` is NOT read from config — the tick
    authorizes as the service and that door is pinned to ``RASK_INGEST_SERVICE_PROJECT``, so taking a
    project here would let config name one the door then refuses.
    """
    kind = os.getenv("RASK_INGEST_CRON_KIND", "").strip()
    dataset = os.getenv("RASK_INGEST_CRON_DATASET", "").strip()
    if not kind or not dataset:
        return None
    raw = os.getenv("RASK_INGEST_CRON_OPTIONS", "").strip()
    try:
        options = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        # A malformed options blob must not silently ingest with none: the source would enumerate
        # something other than what the operator configured, which is worse than not running.
        logger.error("ingest_cron_options_unparsable", extra={"raw_length": len(raw)})
        return None
    return {
        "kind": kind,
        "project": os.getenv("RASK_INGEST_SERVICE_PROJECT", "demo"),
        "dataset": dataset,
        "options": options if isinstance(options, dict) else {},
    }


async def _on_tick(request: Request, _: Annotated[None, Depends(require_dapr_token)]) -> dict[str, str]:
    """One poll — dispatched through the SAME path the HTTP door uses.

    `dispatch_run` is shared deliberately: a second way to start a run would have to re-derive the
    deterministic run id, the idempotency dedupe and the claim/release around dispatch, and two
    implementations of that agree only until one of them changes.

    A FRESH IDEMPOTENCY KEY PER TICK, which is the opposite of the HTTP door's contract and correct
    for the same reason. A caller retries with the same key because it means "the run I already
    asked for"; a tick means "whatever is new since last time", so a fixed key would dedupe every
    poll onto one run forever and a clock-derived one is exactly what the anti-join then filters
    down to nothing new. The tick is cheap when nothing changed — `units_total == 0` short-circuits
    to COMPLETE with no Lance version and no cascade.

    ANSWERS SUCCESS WHATEVER HAPPENS, and that is the contract rather than laziness. A cron binding
    has no caller to report to and Dapr retries a failure, so a tick that raised would re-fire the
    same poll against a source that has not changed. Real ingest failures surface where ingest
    failures always do — the run record and the lineage graph — not by wedging the schedule.
    """
    spec = cron_request()
    if spec is None:
        logger.info("ingest_cron_tick_unconfigured")
        return {"status": "SUCCESS", "detail": "no source configured (RASK_INGEST_CRON_KIND/DATASET)"}

    from fastapi import Response

    from ingest.api import IngestRequest, dispatch_run

    state = request.app.state
    try:
        accepted = await dispatch_run(
            IngestRequest.model_validate(spec),
            Response(),
            store=state.run_store,
            starter=state.workflow_starter,
            # A tick is a new poll, never a retry of the last one — see the docstring.
            idempotency_key=f"cron-{uuid.uuid4().hex}",
            # No user fired this, so there is nobody to name. An invented originator would put a row
            # in an inbox belonging to whoever the literal happened to match.
            originator=None,
        )
    except Exception as exc:
        logger.warning("ingest_cron_tick_failed", extra={"kind": spec["kind"], "dataset": spec["dataset"]}, exc_info=True)
        return {"status": "SUCCESS", "detail": f"tick did not dispatch: {type(exc).__name__}: {str(exc)[:200]}"}
    logger.info("ingest_cron_tick", extra={"run_id": accepted.run_id, "kind": spec["kind"], "dataset": spec["dataset"]})
    return {"status": "SUCCESS", "detail": f"dispatched {accepted.run_id}"}


async def _ack_binding() -> dict[str, str]:
    """Dapr's binding-discovery pre-flight. Without an ack the sidecar logs the binding as
    unregistered and never delivers a single tick."""
    return {"status": "ok"}


def build_incremental_cron_router(binding_name: str) -> APIRouter:
    """Register the tick at the exact binding name the sidecar delivers to (POST poll, OPTIONS ack).

    ROOT-mounted, not under the api prefix: Dapr delivers an input binding to ``POST /<component
    name>`` at the pod root, so the component name, the env var and the served path are one string.
    """
    router = APIRouter()
    router.add_api_route(f"/{binding_name}", _on_tick, methods=["POST"], tags=["ingest-cron"])
    router.add_api_route(f"/{binding_name}", _ack_binding, methods=["OPTIONS"], include_in_schema=False)
    return router


def mount_incremental_cron(target: FastAPI, binding_name: str | None) -> bool:
    """Mount the tick iff a Dapr cron binding names it; return whether it mounted.

    Unmounted by default: a cron route with no cron behind it is a door into starting ingest runs
    that exists for no reason, and the token guard is the only thing in front of it.
    """
    if not binding_name:
        return False
    target.include_router(build_incremental_cron_router(binding_name))
    return True
