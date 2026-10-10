"""The held-promotion door: the bus ingress that starts a review, and the route a person answers it on.

The review is driven through the `SagaClient` port (`service_kit.lakehouse.saga`), which the producer's
lifespan builds once as `app.state.saga_client`; this module names no workflow engine.

**Why this lives on `medallion-producer` and not on the stage runner that held the promotion.**
A signal to the review (Dapr's `raise_workflow_event`) resolves the workflow actor through the app-id of
the process that sends it, so the route and the workflow instance must be in the SAME app. The quality gate runs in the
`silver-to-gold` stage runner — a bus-only worker with no gateway row and no Ingress path — and giving it one
would make a cascade stage publicly addressable to expose a single button. Hosting only the ROUTE here
is worse than either: the sidecar looks for the instance under this app-id, does not find it, and
accepts the call anyway. The operator sees their approval succeed and the promotion expires regardless.

So the workflow is hosted HERE, beside the door, and the stage runner reaches it the way it reaches every
other stage — by publishing an event.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Protocol

from dapr.ext.fastapi import DaprApp
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import InvalidTableStateError, PermissionDeniedError, ServiceUnavailableError, TableNotFoundError
from pydantic import BaseModel, ValidationError

from medallion.api.dependencies import SettingsDep
from medallion.api.produce_auth import authenticate_subject
from medallion.core.config import get_settings
from medallion.schemas.promotion import PromotionSpec
from service_kit.draining import retry_when_draining
from service_kit.governed import fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, audit
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.governed.signing_key import retry_until_signed
from service_kit.lakehouse.saga import SagaClient, SagaStart


#: Ceiling on any single synchronous saga-client call from these routes.
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
    """Run a blocking saga-client call off the loop, with a ceiling.

    A `TimeoutError` is left to propagate to the route, which maps it to 503 — the caller's request is
    fine, the engine is simply not answering.
    """
    return await asyncio.wait_for(run_in_threadpool(call, *args), timeout=WORKFLOW_CALL_TIMEOUT_SECONDS)


log = logging.getLogger(__name__)
router = APIRouter(tags=["promotions"])

_SUCCESS = {"status": "SUCCESS"}
_RETRY = {"status": "RETRY"}
_DROP = {"status": "DROP"}


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

    Deterministic because it is the only handle either side has: the stage runner publishes a hold and moves
    on, and the door receives an id from a URL. A redelivered hold must re-attach to the review that
    is already open rather than asking the approver a second time.

    The token rides unfolded, `.` and `:` included. The durabletask gRPC path the SDK starts it on
    checks no id shape; Dapr's own workflow API would refuse to START it (letters, digits, `-`, `_`,
    <= 64 only; `pkg/api/universal/workflow.go`, read at dapr v1.18.1) and manages any existing id.
    """
    return f"promotion-{token}"


def _live_spec(client: SagaClient, instance_id: str) -> PromotionSpec:
    """Load the promotion behind `instance_id`, refusing anything this app cannot actually resume.

    A live instance whose stored input no longer validates is `InvalidTableState`: the review exists
    and its state cannot serve the request, and a retry reads the same bytes. The response carries no
    internals, so the log names the instance and the failing fields for the operator who must clear it.
    """
    state = client.state(instance_id)
    if state is None:
        raise TableNotFoundError(f"no promotion under review with id {instance_id!r}")
    if not state.status.live:
        # A finished review is never answerable: the engine accepts a signal for a completed instance
        # and discards it, which is the silent success this door exists to refuse.
        raise TableNotFoundError(f"promotion {instance_id!r} is no longer under review ({state.status.name})")
    try:
        return PromotionSpec.model_validate_json(state.input or "{}")
    except ValidationError as exc:
        log.warning(
            "medallion_promotion_review_unreadable",
            extra={"instance_id": instance_id, "fields": [".".join(str(part) for part in error["loc"]) for error in exc.errors()]},
        )
        raise InvalidTableStateError(f"promotion {instance_id!r} is under review but its stored input can no longer be read") from exc


def promotion_object(spec: PromotionSpec) -> str:
    """The FGA object a decision is gated on: the DESTINATION stage of the promotion.

    `can_promote: validator` is a rung on the namespace being promoted INTO — a writer may write
    within a stage without being able to promote into a gated one. `to_namespace` is used as given:
    it is the namespace the stage runner resolved and checked its own rung on, so the approver is
    gated on the same object the grants name.
    """
    return f"namespace:{spec.to_namespace}"


async def handle_promotion_held(event: dict[str, Any], *, client: SagaClient | None) -> dict[str, str]:
    """Turn a stage runner's held promotion into a durable review instance. Testable half of the subscription.

    ``client`` is ``None`` when this producer hosts no review runtime (quality review off): the hold is DROPPED to the
    dead-letter topic, where it is counted and visible, rather than scheduled onto an engine nothing runs.
    """
    if client is None:
        log.error("medallion_promotion_held_without_review", extra={"event": str(event)[:512]})
        return _DROP
    try:
        spec = PromotionSpec.model_validate((event or {}).get("data") or {})
    except ValidationError:
        # Untrusted bus input: a shape that will not parse now will not parse on redelivery either,
        # so retrying it parks a poison message on the topic forever.
        log.warning("medallion_promotion_hold_malformed", extra={"event": str(event)[:512]})
        return _DROP

    # The workflow FUNCTION is resolved here, not at module scope: `medallion.workflow` is the engine
    # adapter (its body is `import dapr.ext.workflow as wf`), and importing it from a router the
    # producer always mounts is what made the cascade head depend on the engine.
    from medallion.workflow import promotion_review

    instance_id = instance_for(spec.token)
    try:
        handle = await _bounded(lambda: client.start(saga=promotion_review, payload=spec.model_dump(), instance_id=instance_id))
    except Exception:
        # `start` already told "the review is open" (ALREADY_RUNNING) from "nothing is holding the
        # promotion" (it raised): no sidecar, an unscoped actor state store, the engine down. Acking the
        # second would lose the review.
        log.warning("medallion_promotion_review_not_scheduled", extra={"token": spec.token, "instance_id": instance_id}, exc_info=True)
        return _RETRY
    if handle.outcome is SagaStart.ALREADY_RUNNING:
        log.info("medallion_promotion_review_reattach", extra={"instance_id": instance_id})
        return _SUCCESS
    log.info("medallion_promotion_review_scheduled", extra={"token": spec.token, "instance_id": instance_id, "dataset": spec.to_dataset})
    return _SUCCESS


async def decide_promotion(
    instance_id: str,
    *,
    approved: bool,
    subject: str,
    client: SagaClient | None,
    authorize: Authorize | None = None,
) -> dict[str, Any]:
    """Deliver one person's answer to a held promotion.

    Checks the instance is hosted and still live BEFORE signalling it, because the engine accepts a
    signal for an instance it does not host and discards it — a 202 for an approval that
    will never arrive is the one outcome worse than a 404.
    """
    if client is None:
        raise TableNotFoundError(f"no promotion is under review with id {instance_id!r}: quality review is not enabled on this estate")
    if not subject:
        # The shared-token path of the auth door resolves no principal. The workflow refuses an
        # unattributable decision anyway; refusing it here says so to the caller instead of three
        # hops later in a lineage FAIL nobody is watching.
        raise PermissionDeniedError("a promotion decision must name the person who made it; sign in and retry")

    try:
        spec = await _bounded(_live_spec, client, instance_id)
    except (TableNotFoundError, PermissionDeniedError, InvalidTableStateError):
        raise
    except Exception as exc:
        raise ServiceUnavailableError("the workflow engine is not available") from exc

    if authorize is not None:
        await authorize(subject=subject, obj=promotion_object(spec))

    await _bounded(lambda: client.signal(instance_id, "promotion_decision", {"approved": approved, "subject": subject}))
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
    # non-admin validator the rung exists for (docs/adr/0131-annotations-are-derived-and-readiness-is-the-published-tag.md).
    #
    # A service token resolves no subject here, and `decide_promotion` refuses that: the estate's
    # shared credential cannot approve its own output, and the gateway's daprd-stamped token — the
    # measured bypass on the sibling /produce door — buys a caller nothing on this route.
    subject: Annotated[str | None, Depends(authenticate_subject)],
) -> DecisionAccepted:
    """Approve or reject a held promotion. 403 without a signed-in `can_promote` holder on the
    destination stage; 404 when this app hosts no live review under that id; 409 when the review's
    stored input can no longer be read."""
    outcome = await decide_promotion(
        instance_id,
        approved=body.approved,
        subject=subject or "",
        client=request.app.state.saga_client,
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
    client = request.app.state.saga_client
    if client is None:
        raise TableNotFoundError(f"no promotion is under review with id {instance_id!r}: quality review is not enabled on this estate")
    spec = await _bounded(_live_spec, client, instance_id)
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
    """Subscribe to the stage runners' held-promotion topic (reusing the producer's ``DaprApp``)."""
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
        request: Request,
        config: SettingsDep,
        _: Annotated[None, Depends(require_dapr_token)],
        drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None,
        signing: Annotated[dict[str, str] | None, Depends(retry_until_signed)] = None,
    ) -> dict[str, str]:
        """Thin wrapper over the testable :func:`handle_promotion_held`. Token-guarded: a forged hold
        would park a promotion nobody asked for and name an approver who never agreed to be asked."""
        if drain is not None:
            return drain
        if signing is not None:
            return signing
        return await handle_promotion_held(event, client=request.app.state.saga_client)

    return dapr_app
