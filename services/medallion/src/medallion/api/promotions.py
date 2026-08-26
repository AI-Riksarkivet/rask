"""The held-promotion door: the bus ingress that starts a review, and the route a person answers it on.

**Why this lives on `medallion-producer` and not on the mover that held the promotion.**
`raise_workflow_event` resolves the workflow actor through the app-id of the process that calls it, so
the route and the workflow instance must be in the SAME app. The quality gate runs in the
`silver-to-gold` mover — a bus-only worker with no gateway row and no Ingress path — and giving it one
would make a cascade stage publicly addressable to expose a single button. Hosting only the ROUTE here
is worse than either: the sidecar looks for the instance under this app-id, does not find it, and
accepts the call anyway. The operator sees their approval succeed and the promotion expires regardless.

So the workflow is hosted HERE, beside the door, and the mover reaches it the way it reaches every
other stage — by publishing an event.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any, Protocol

from dapr.ext.fastapi import DaprApp
from dapr.ext.workflow.workflow_state import WorkflowStatus
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, TableNotFoundError
from pydantic import BaseModel, ValidationError

from medallion.api.dependencies import SettingsDep
from medallion.api.produce_auth import authenticate_subject
from medallion.core.config import get_settings
from medallion.workflow import PromotionSpec, promotion_review
from service_kit.draining import retry_when_draining
from service_kit.governed import fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, audit
from service_kit.governed.dapr_auth import require_dapr_token


#: Ceiling on any single synchronous workflow-client call from these routes.
#:
#: `run_in_threadpool` around a blocking SDK call is the estate's sanctioned pattern, but unbounded it
#: parks a worker thread forever against a sidecar that ACCEPTS and never answers — and the threadpool
#: is finite and shared. Bounding it is symmetry with the one place the estate already bounds
#: (`SCHEDULE_TIMEOUT_SECONDS` on the ingest schedule path).
#:
#: A timeout answers 503 + Retry-After rather than 500, and that is honest HERE specifically because
#: `instance_for(token)` is deterministic: a retried decision converges on the same instance instead
#: of forking a second one.
WORKFLOW_CALL_TIMEOUT_SECONDS = 5.0


async def _bounded(call: Any, *args: Any) -> Any:
    """Run a blocking workflow-client call off the loop, with a ceiling.

    A `TimeoutError` is left to propagate to the route, which maps it to 503 — the caller's request is
    fine, the engine is simply not answering.
    """
    return await asyncio.wait_for(run_in_threadpool(call, *args), timeout=WORKFLOW_CALL_TIMEOUT_SECONDS)


log = logging.getLogger(__name__)
router = APIRouter(tags=["promotions"])

_SUCCESS = {"status": "SUCCESS"}
_RETRY = {"status": "RETRY"}
_DROP = {"status": "DROP"}

#: A workflow instance is never terminal-and-answerable: the engine accepts an event for a completed
#: instance and discards it, which is the silent-success this door exists to refuse.
_LIVE = (WorkflowStatus.RUNNING, WorkflowStatus.PENDING, WorkflowStatus.SUSPENDED)


class _Client(Protocol):
    """The slice of `DaprWorkflowClient` this module uses, so a test needs no sidecar."""

    def raise_workflow_event(self, instance_id: str, event_name: str, *, data: Any = None) -> None: ...
    def schedule_new_workflow(self, *, workflow: Any, input: Any, instance_id: str) -> str: ...  # noqa: A002
    def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> Any: ...


class Authorize(Protocol):
    def __call__(self, *, subject: str, obj: str) -> Any: ...


class DecisionAccepted(BaseModel):
    """What the approver gets back. A declared return type validates, filters and documents it —
    `dict[str, Any]` did none of those and put nothing in the OpenAPI schema."""

    status: str
    instance_id: str
    approved: bool
    dataset: str


class PromotionUnderReview(BaseModel):
    """What is being asked, so the approver can answer it."""

    instance_id: str
    project: str
    from_dataset: str
    to_dataset: str
    reasons: list[str]
    approval_hours: int


class DecisionRequest(BaseModel):
    """The whole body. WHO decided comes from the verified bearer, never from the caller's JSON."""

    approved: bool


def instance_for(token: str) -> str:
    """The workflow instance id for a promotion, derived from the run token.

    Deterministic because it is the only handle either side has: the mover publishes a hold and moves
    on, and the door receives an id from a URL. A redelivered hold must re-attach to the review that
    is already open rather than asking the approver a second time.
    """
    return f"promotion-{token}"


def _client(client: _Client | None) -> _Client:
    if client is not None:
        return client
    import dapr.ext.workflow as wf

    return wf.DaprWorkflowClient()


def _live_spec(client: _Client, instance_id: str) -> PromotionSpec:
    """Load the promotion behind `instance_id`, refusing anything this app cannot actually resume."""
    state = client.get_workflow_state(instance_id, fetch_payloads=True)
    if state is None:
        raise TableNotFoundError(f"no promotion under review with id {instance_id!r}")
    status = getattr(state, "runtime_status", None)
    if status not in _LIVE:
        name = getattr(status, "name", str(status))
        raise TableNotFoundError(f"promotion {instance_id!r} is no longer under review ({name})")
    return PromotionSpec.model_validate(json.loads(state.serialized_input or "{}"))


def promotion_object(spec: PromotionSpec) -> str:
    """The FGA object a decision is gated on: the DESTINATION stage of the promotion.

    `can_promote: validator` is a rung on the namespace being promoted INTO — a writer may write
    within a stage without being able to promote into a gated one. Project-qualified the same way
    lineage's audience is, and left bare on a projectless estate (#84), where a prefix would name an
    object no tuple mentions.
    """
    ns = spec.to_namespace
    if spec.project and not ns.startswith(f"{spec.project}-"):
        ns = f"{spec.project}-{ns}"
    return f"namespace:{ns}"


async def handle_promotion_held(event: dict[str, Any], *, client: _Client | None = None) -> dict[str, str]:
    """Turn a mover's held promotion into a durable review instance. Testable half of the subscription."""
    try:
        spec = PromotionSpec.model_validate((event or {}).get("data") or {})
    except ValidationError:
        # Untrusted bus input: a shape that will not parse now will not parse on redelivery either,
        # so retrying it parks a poison message on the topic forever.
        log.warning("medallion_promotion_hold_malformed", extra={"event": str(event)[:512]})
        return _DROP

    wf_client = _client(client)
    instance_id = instance_for(spec.token)
    try:
        await _bounded(lambda: wf_client.schedule_new_workflow(workflow=promotion_review, input=spec.model_dump(), instance_id=instance_id))
    except Exception:
        # Two events wear one exception, and they need opposite answers. An instance that already
        # exists means the review is open and this delivery is fully handled. Anything else — no
        # sidecar, an unscoped actor state store, the engine down — means NOTHING is holding the
        # promotion, and acking would lose the review.
        if not _exists(wf_client, instance_id):
            log.warning("medallion_promotion_review_not_scheduled", extra={"token": spec.token, "instance_id": instance_id}, exc_info=True)
            return _RETRY
        log.info("medallion_promotion_review_reattach", extra={"instance_id": instance_id})
        return _SUCCESS
    log.info("medallion_promotion_review_scheduled", extra={"token": spec.token, "instance_id": instance_id, "dataset": spec.to_dataset})
    return _SUCCESS


def _exists(client: _Client, instance_id: str) -> bool:
    """Whether the engine knows this instance — with an unanswerable lookup read as 'no'.

    A state read that raises means the engine is unreachable, which is the case that must RETRY;
    returning False sends the caller down exactly that path.
    """
    try:
        return client.get_workflow_state(instance_id) is not None
    except Exception:
        return False


async def decide_promotion(
    instance_id: str,
    *,
    approved: bool,
    subject: str,
    client: _Client | None = None,
    authorize: Authorize | None = None,
) -> dict[str, Any]:
    """Deliver one person's answer to a held promotion.

    Checks the instance is hosted and still live BEFORE touching the client, because the client
    accepts an event for an instance it does not host and discards it — a 202 for an approval that
    will never arrive is the one outcome worse than a 404.
    """
    if not subject:
        # The shared-token path of the auth door resolves no principal. The workflow refuses an
        # unattributable decision anyway; refusing it here says so to the caller instead of three
        # hops later in a lineage FAIL nobody is watching.
        raise PermissionDeniedError("a promotion decision must name the person who made it; sign in and retry")

    wf_client = _client(client)
    try:
        spec = await _bounded(_live_spec, wf_client, instance_id)
    except (TableNotFoundError, PermissionDeniedError):
        raise
    except Exception as exc:
        raise ServiceUnavailableError("the workflow engine is not available") from exc

    if authorize is not None:
        await authorize(subject=subject, obj=promotion_object(spec))

    await _bounded(lambda: wf_client.raise_workflow_event(instance_id, "promotion_decision", data={"approved": approved, "subject": subject}))
    log.info(
        "medallion_promotion_decided",
        extra={"instance_id": instance_id, "approved": approved, "subject": subject, "dataset": spec.to_dataset},
    )
    return {"status": "accepted", "instance_id": instance_id, "approved": approved, "dataset": spec.to_dataset}


def _fga_gate(request: Request) -> Authorize | None:
    """Build the `can_promote` check, or `None` when FGA is off (dev-open, as everywhere else here)."""
    fga_client = getattr(request.app.state, "fga", None)
    if fga_client is None:
        return None

    async def _check(*, subject: str, obj: str) -> None:
        try:
            allowed = await fga.check(fga_client, user=subject, relation="can_promote", obj=obj)
        except ServiceUnavailableError:
            audit("can_promote", FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
            raise ServiceUnavailableError("authorization service is not available") from None
        audit("can_promote", ALLOW if allowed else DENY, subject=subject, resource=obj)
        if not allowed:
            raise PermissionDeniedError(f"{subject} lacks can_promote on {obj}")

    return _check


@router.post("/promotions/{instance_id}/decision", status_code=202)
async def decide(
    instance_id: str,
    body: DecisionRequest,
    request: Request,
    # AUTHENTICATION ONLY. The rung for this act is `can_promote: validator`, checked below against
    # the promotion's own destination — deliberately NOT /produce's `can_administer` on a
    # chart-configured project, which is coarser AND different, and would lock out exactly the
    # non-admin validator the rung exists for (docs/architecture/ingest-and-tier-movement.md §4).
    #
    # A service token resolves no subject here, and `decide_promotion` refuses that: the estate's
    # shared credential cannot approve its own output, and the gateway's daprd-stamped token — the
    # measured bypass on the sibling /produce door — buys a caller nothing on this route.
    subject: Annotated[str | None, Depends(authenticate_subject)],
) -> DecisionAccepted:
    """Approve or reject a held promotion. 403 without a signed-in `can_promote` holder on the
    destination stage; 404 when this app hosts no live review under that id."""
    outcome = await decide_promotion(
        instance_id,
        approved=body.approved,
        subject=subject or "",
        # THE LIFESPAN'S CLIENT, like `show` 13 lines below — whose comment states the rule this route
        # was breaking beside it. Omitting it made `_client(None)` construct a fresh
        # `DaprWorkflowClient`, and so a fresh gRPC channel to the sidecar, on every approval.
        client=getattr(request.app.state, "workflow_client", None),
        authorize=_fga_gate(request),
    )
    return DecisionAccepted(**outcome)


@router.get("/promotions/{instance_id}")
async def show(
    instance_id: str,
    request: Request,
    subject: Annotated[str | None, Depends(authenticate_subject)],
) -> PromotionUnderReview:
    """What is being asked, so the approver can answer it: the datasets, the failed assertions, the deadline."""
    # From `app.state`, built once in the lifespan. Constructing a client per request re-opens its
    # connection to the sidecar on every call — the "build it in lifespan, inject it" rule.
    wf_client = _client(getattr(request.app.state, "workflow_client", None))
    spec = await _bounded(_live_spec, wf_client, instance_id)
    gate = _fga_gate(request)
    if gate is not None:
        # `and subject` used to sit here, which read as a guard and acted as a bypass: a caller with
        # NO credential resolves `subject=None`, so the gate was SKIPPED rather than failed and the
        # promotion's datasets and failed assertions came back 200. `authenticate_subject`'s own
        # docstring states the contract this now keeps -- "a caller with no verified identity gets
        # `None` and the door refuses" -- and `decide`, on this router, already refused exactly here.
        if not subject:
            raise PermissionDeniedError("reading a held promotion requires a signed-in caller; sign in and retry")
        await gate(subject=subject, obj=promotion_object(spec))
    return PromotionUnderReview(
        instance_id=instance_id,
        project=spec.project,
        from_dataset=spec.from_dataset,
        to_dataset=spec.to_dataset,
        reasons=spec.reasons,
        approval_hours=spec.approval_hours,
    )


def register_promotion_route(app: FastAPI, dapr_app: DaprApp | None = None) -> DaprApp:
    """Subscribe to the movers' held-promotion topic (reusing the producer's ``DaprApp``)."""
    settings = get_settings()
    dapr_app = dapr_app or DaprApp(app)

    @dapr_app.subscribe(
        pubsub=settings.pubsub,
        topic=settings.promotion_topic,
        route="/promotion-held",
        dead_letter_topic=settings.dlq_topic or None,
    )
    async def on_promotion_held(
        event: dict[str, Any],
        config: SettingsDep,
        _: Annotated[None, Depends(require_dapr_token)],
        drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None,
    ) -> dict[str, str]:
        """Thin wrapper over the testable :func:`handle_promotion_held`. Token-guarded: a forged hold
        would park a promotion nobody asked for and name an approver who never agreed to be asked."""
        if drain is not None:
            return drain
        return await handle_promotion_held(event)

    return dapr_app
