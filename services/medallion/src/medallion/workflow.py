"""The medallion's one Dapr workflow: the promotion review.

No Ray lane hosts a workflow: a stage job and a training job are each PLANNED and reach their terminal through an
outcome door and the plan sweep (`medallion.services.stage_plans`, `medallion.services.train_plans`, CP-029). What
remains here runs in the PRODUCER's runtime: `promotion_review` holds a promotion until a person decides.

DETERMINISM (checked against the dapr-skills Python checklist, DWF-DET-001..015). No wall clock — a deadline is
derived from `ctx.current_utc_datetime`. No sleep — `ctx.create_timer`. No I/O, no `os.environ`, no randomness in a
workflow body; every one of those lives in an activity. Logging in a body is guarded by `not ctx.is_replaying`.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

import dapr.ext.workflow as wf
from pydantic import BaseModel, Field

from medallion.core.best_effort import best_effort
from medallion.core.lineage_publish import emit_lineage, require_signing_key, signed_control_event
from medallion.core.metrics import record_promotion_outcome
from medallion.schemas.promotion import PromotionSpec
from service_kit.activity_loop import run_activity
from service_kit.governed.signing_key import MAX_REFRESH_SECONDS


if TYPE_CHECKING:
    from collections.abc import Generator

    from dapr.ext.workflow import DaprWorkflowContext, WorkflowActivityContext


log = logging.getLogger(__name__)


#: Retry for the activities. Matches ingest's policy in shape and reasoning: a transient dashboard or
#: broker blip must not fail a stage that is running fine, and an activity that keeps failing must
#: eventually surface rather than retry forever.
ACTIVITY_RETRY: Final = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=2),
    max_number_of_attempts=5,
    backoff_coefficient=2.0,
    max_retry_interval=timedelta(seconds=60),
)

#: How often the producer's key holder re-reads a key it has not resolved: `SigningKeyHolder`'s `retry_seconds`, which
#: `make_signing_holder` leaves at its default.
_KEY_HOLDER_RETRY_SECONDS: Final = 15

#: Retry for an activity that SIGNS what it sends: `request_approval` and `emit_promotion_outcome` ([[XC-078]]). Their
#: failure to plan for is a key miss, which is the producer's holder still resolving and heals in place, so the budget is
#: sized from the holder rather than from a blip. Attempts run at the holder's own cadence for the window in which every
#: signer is expected to have healed: one `MAX_REFRESH_SECONDS` (values.yaml `signing:`, STORE LOSS) plus one re-read.
#: That is 2 + 300 // 15 = 22 attempts, the last starting 21 x 15 s = 315 s after the first. `ACTIVITY_RETRY` stops after
#: 2 + 4 + 8 + 16 = 30 s, which a store outage across a restart outlasts.
SIGNING_ACTIVITY_RETRY: Final = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=_KEY_HOLDER_RETRY_SECONDS),
    max_number_of_attempts=2 + int(MAX_REFRESH_SECONDS) // _KEY_HOLDER_RETRY_SECONDS,
    backoff_coefficient=1.0,
)

#: Assertion failures that are CORRUPTION rather than a judgement call. A blob pointer that does not
#: resolve and a null key column are structurally wrong — no approval makes the data right, so nobody
#: is asked. Every other failure is the archive's call.
_STRUCTURAL_FAILURES: Final = frozenset({"blob_resolves", "not_null"})


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Activities — everything non-deterministic lives below this line
# ─────────────────────────────────────────────────────────────────────────────────────────────────


# --------------------------------------------------------------------------------------------------
# ACTIVITY ENVELOPES (DWF-ACT-009)
#
# dapr-ext-workflow 1.18 coerces an activity's input into whatever model its second parameter names —
# `workflow_runtime._coerce_activity_input` -> `_model_protocol.coerce_to_model` — so these are
# enforced by the runtime rather than documentation. Every activity below took `dict[str, Any]`, and
# each one re-validated by hand or read raw keys; the envelopes make the shape the signature.
#
# Owner ruling 2026-08-25 ("do what best practices"), which overrides the estate's earlier written
# position in `flows/activities.py` — that a plain dict survives a model gaining a field. The
# back-compat argument behind it is waived, and the SDK has moved on besides.
# --------------------------------------------------------------------------------------------------


class PromotionOutcome(BaseModel):
    """What `emit_promotion_outcome` records. `status` is REQUIRED — it decides whether the lineage
    event is a COMPLETE or a FAIL, so a default would silently pick one.

    The other two carry honest defaults rather than lazy ones: a BLOCKED or EXPIRED promotion has no
    decider, and an approved one has no reasons.
    """

    status: str
    decided_by: str | None = None
    reasons: list[str] = Field(default_factory=list)


class PromotionReport(BaseModel):
    """`emit_promotion_outcome`: the promotion and the decision reached on it."""

    spec: PromotionSpec
    outcome: PromotionOutcome


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────────────────────────


def _run_async(coro: Any) -> Any:  # noqa: ANN401 — mirrors ingest.workflow._run_async
    """Run a coroutine from an activity's worker thread, on the worker's ONE loop.

    Not `asyncio.run`: a client pooled for the worker's lifetime holds keep-alive connections that
    belong to the loop that opened them, so a fresh loop per activity hands the next one a connection
    bound to a dead loop (`Event loop is closed`). See `service_kit.activity_loop`: the loop has the
    same lifetime a pooled client claims.
    """
    return run_activity(coro)


def register(runtime: wf.WorkflowRuntime) -> None:
    """Register everything with the runtime — one place, so nothing is silently unregistered.

    THE `<verb>_activity` SUFFIX IS DELIBERATELY NOT USED (DWF-ACT-008, owner ruling 2026-08-25).
    `register_activity` takes no explicit name, so the runtime registers by `__name__` and these
    function names ARE the wire names. Renaming them would therefore break replay for every in-flight
    instance — the estate has no versioning seam — and the convention buys nothing here: the registry
    is single-sourced through this function, and nothing cross-language calls these activities.

    Recorded rather than left silent, because a sweep that finds no reasoning re-raises the finding.
    """
    for w in WORKFLOWS:
        runtime.register_workflow(w)
    for a in ACTIVITIES:
        runtime.register_activity(a)


def start_runtime() -> wf.WorkflowRuntime:
    """Build, register and start the runtime that hosts this module's workflows, and return it to be shut down.

    Here rather than in the producer's lifespan so the engine's runtime is named only by its adapter: the producer
    asks for a running worker and holds the handle. `start()` runs the worker on its own threads and does not block
    the event loop.
    """
    runtime = wf.WorkflowRuntime()
    register(runtime)
    runtime.start()
    return runtime


# --------------------------------------------------------------------------- #
# S3/S4 — the quality gate's third answer: a held promotion a person can release
# --------------------------------------------------------------------------- #


def promotion_review(ctx: DaprWorkflowContext, payload: dict[str, Any]) -> Generator[Any, Any, dict[str, Any]]:
    """Hold a promotion, ask a person, resume the cascade only on an explicit yes.

    A workflow rather than a table and a cron because the wait is hours-to-days and must survive every
    pod restart in between: an approval is a plan worth resuming, since losing it loses a decision.

    EVERY path that is not an explicit clean verdict or an explicit approval BLOCKS. A held batch that
    reached promotion by falling off the end of a branch would be the gate failing open, which is
    worse than the permanent DROP this replaces.
    """
    # The WORKFLOW body validates by hand. The SDK coerces an ACTIVITY's input into its annotated
    # model; a workflow body's input is NOT coerced, so this line is what makes `spec` a model.
    spec = PromotionSpec.model_validate(payload)

    # Resolved by an ACTIVITY, and first: a threshold read in the body changes under a running
    # instance and makes replay disagree with the original turn.
    policy = yield ctx.call_activity(resolve_review_policy, input=spec, retry_policy=ACTIVITY_RETRY)
    verdict = policy.get("verdict", "block")

    if verdict == "block":
        yield from _record_outcome(ctx, PromotionReport(spec=spec, outcome=PromotionOutcome(status="BLOCKED", reasons=spec.reasons)))
        return {"status": "BLOCKED", "decided_by": None, "reasons": spec.reasons}

    decided_by: str | None = None
    if verdict == "review":
        # ASK BEFORE WAITING, and treat an unsendable ask as a refusal: parking on an event nobody was
        # told about is an outage wearing a pause. An ask the producer could not sign within its budget is one too, so
        # this boundary records it BLOCKED with the reason, where the raise would end the instance FAILED with the hold
        # acknowledged and nobody told.
        reasons = [*spec.reasons, "no reachable approver"]
        try:
            asked = yield ctx.call_activity(request_approval, input=spec, retry_policy=SIGNING_ACTIVITY_RETRY)
        except Exception as exc:  # noqa: BLE001 — the boundary is the point; an ask that never left must still reach lineage
            asked = False
            reasons.append(str(exc) or exc.__class__.__name__)
        if not asked:
            yield from _record_outcome(ctx, PromotionReport(spec=spec, outcome=PromotionOutcome(status="BLOCKED", reasons=reasons)))
            return {"status": "BLOCKED", "decided_by": None, "reasons": reasons}

        approval = ctx.wait_for_external_event("promotion_decision")
        deadline = ctx.create_timer(timedelta(hours=spec.approval_hours))
        winner = yield wf.when_any([approval, deadline])

        if winner is deadline:
            yield from _record_outcome(ctx, PromotionReport(spec=spec, outcome=PromotionOutcome(status="EXPIRED", reasons=spec.reasons)))
            return {"status": "EXPIRED", "decided_by": None, "reasons": spec.reasons}

        decision = approval.get_result() or {}
        decided_by = decision.get("subject")
        # The DOOR authenticates; this only refuses a decision that names nobody, or one whose subject
        # is the producing service approving its own output.
        if not decided_by or decided_by == settings_author_marker(spec) or not decision.get("approved"):
            status = "REJECTED" if decided_by and decision.get("approved") is False else "BLOCKED"
            yield from _record_outcome(ctx, PromotionReport(spec=spec, outcome=PromotionOutcome(status=status, decided_by=decided_by, reasons=spec.reasons)))
            return {"status": status, "decided_by": decided_by, "reasons": spec.reasons}

    # UNCONDITIONAL: every approval resumes through the catalog, because the tag move IS the
    # promotion on every tier — the terminal ones included, and silver-to-gold is both terminal and the
    # tier the chart gates on `can_promote`.
    # AN ERROR BOUNDARY: the only durable
    # record of this decision is written BELOW, and an exhausted retry policy raising through here
    # took the instance terminal FAILED and skipped it. `publish_stage_output` raises `RegisterError`
    # on any catalog 4xx/5xx, and the reachable trigger is not transient -- the approval window is 72
    # hours by default, and a version published while the approver deliberated makes the catalog
    # refuse to move `published` backwards, identically on all five attempts.
    #
    # Recorded as its own status, never swallowed into PROMOTED: the tag did NOT move. A lying audit
    # trail is worse than the crash it replaces, because nothing downstream can detect it.
    #
    # `str(exc)` is replay-safe -- a failed activity's message is reconstructed from history, so a
    # replay derives the same reason string rather than re-running the catalog call.
    promotion_failure: str | None = None
    try:
        yield ctx.call_activity(publish_promotion, input=spec, retry_policy=ACTIVITY_RETRY)
    except Exception as exc:  # noqa: BLE001 — the boundary is the point; any activity failure must still reach lineage
        promotion_failure = str(exc) or exc.__class__.__name__

    if promotion_failure is not None:
        reasons = [*spec.reasons, promotion_failure]
        yield from _record_outcome(ctx, PromotionReport(spec=spec, outcome=PromotionOutcome(status="PROMOTION_FAILED", decided_by=decided_by, reasons=reasons)))
        return {"status": "PROMOTION_FAILED", "decided_by": decided_by, "reasons": reasons}

    yield from _record_outcome(ctx, PromotionReport(spec=spec, outcome=PromotionOutcome(status="PROMOTED", decided_by=decided_by)))
    return {"status": "PROMOTED", "decided_by": decided_by, "reasons": spec.reasons}


def _record_outcome(ctx: DaprWorkflowContext, report: PromotionReport) -> Generator[Any, Any]:
    """Record one decision in lineage, and never fail the review over the record.

    `emit_promotion_outcome` signs, so it runs under `SIGNING_ACTIVITY_RETRY`. The decision is already made and the review
    returns it whatever happens here, so a record the producer could not sign within the budget is logged, the one trace
    of the decision outside workflow history, rather than raised: a raise would end the instance FAILED with the decision
    unreported. The log is skipped on replay, which would repeat it.
    """
    try:
        yield ctx.call_activity(emit_promotion_outcome, input=report, retry_policy=SIGNING_ACTIVITY_RETRY)
    except Exception as exc:  # noqa: BLE001 — the boundary is the point; the review's outcome stands without its record
        if not ctx.is_replaying:
            log.error(
                "medallion_promotion_outcome_unrecorded",
                extra={"token": report.spec.token, "dataset": report.spec.to_dataset, "status": report.outcome.status, "error": str(exc)},
            )


def settings_author_marker(spec: PromotionSpec) -> str:
    """The producing identity, which must never approve its own promotion.

    Pure and derived from the spec alone so the workflow body stays deterministic — it reads no
    settings and no clock.
    """
    return f"service:{spec.from_namespace}-to-{spec.to_namespace}"


def resolve_review_policy(ctx: WorkflowActivityContext, spec: PromotionSpec) -> dict[str, Any]:
    """Split a hold into corrupt / unusual / clean, and FAIL CLOSED on anything else.

    `blob_resolves` and a null key column are structural: the data is wrong and no approval makes it
    right. Review is opt-in per deployment, because an estate with nobody to ask must keep the old
    behaviour rather than parking promotions on an event no one will raise — and "keep the old
    behaviour" means BLOCK, which is what the gate did before, not promote.
    """
    # Dapr hands an activity the DECODED DICT, not the annotated model: the input crossed the
    # durable boundary as JSON and the SDK never reads the annotation. Coerce before use, or every
    # attribute read below is an AttributeError the moment a real workflow runs it (measured live:
    # `'dict' object has no attribute 'outcome'` killed the cascade's own failure reporter).
    spec = PromotionSpec.model_validate(spec)
    from medallion.core.config import get_settings

    settings = get_settings()
    if not spec.reasons:
        return {"verdict": "promote", "reasons": []}
    if any(reason in _STRUCTURAL_FAILURES for reason in spec.reasons):
        return {"verdict": "block", "reasons": spec.reasons}
    return {"verdict": "review" if settings.quality_review_enabled else "block", "reasons": spec.reasons}


def request_approval(ctx: WorkflowActivityContext, spec: PromotionSpec) -> bool:
    """Tell the approver there is something to decide. Returns whether the ask went out.

    Published DIRECTLY rather than through `process_control_emitter()`: the medallion producer, which hosts the
    `promotion_review` workflow this activity belongs to, sets no process emitter, so that path is a no-op here and
    the ask would be silently swallowed — the exact class of defect this feature exists to fix.

    Raises:
        SigningKeyUnavailableError: the producer signs and has no key, so nothing is sent and the activity is retried.
    """
    # Dapr hands an activity the DECODED DICT, not the annotated model: the input crossed the
    # durable boundary as JSON and the SDK never reads the annotation. Coerce before use, or every
    # attribute read below is an AttributeError the moment a real workflow runs it (measured live:
    # `'dict' object has no attribute 'outcome'` killed the cascade's own failure reporter).
    spec = PromotionSpec.model_validate(spec)
    from dapr.aio.clients import DaprClient

    from medallion.core.config import get_settings
    from service_kit.control_events import CONTROL_TOPIC, CatalogControlEvent
    from service_kit.dapr_publish import publish_event  # noqa: TID251

    if not spec.approver:
        log.warning("medallion_promotion_unapprovable", extra={"dataset": spec.to_dataset, "token": spec.token})
        return False

    settings = get_settings()
    event = CatalogControlEvent(
        action="promotion_review_requested",
        object_type="table",
        # The catalog grants on `table:<catalog id>`, and `to_dataset` IS that id: the stage runner
        # resolved it (env lanes already tenant-qualified, declared lanes exactly as declared).
        object_id=f"table:{spec.to_dataset}",
        actor=None,
        extra={"subject": f"user:{spec.approver}", "reasons": spec.reasons, "project": spec.project, "token": spec.token},
        # DERIVED, not defaulted. `event_id` is documented as the client-side dedupe key, and the
        # model's `uuid4()` default makes it a fresh key on every execution — the one value that
        # cannot dedupe. Dapr re-executes an activity whose result was not recorded, so the approver
        # was asked twice for one promotion. Keyed on the TOKEN rather than a constant: a constant
        # would collapse every promotion onto the first one, which is worse than the bug.
        event_id=f"promotion-review-{spec.token}",
    )
    # SIGNED BEFORE THE PUBLISH IS TRIED, and outside the `try` below that turns a failed publish into a refusal
    # ([[XC-078]]). An enforcing door drops an unsigned ask, so a producer without its key sends nothing: the raise
    # reaches `SIGNING_ACTIVITY_RETRY`, which runs the activity again once the key may have resolved, where returning False
    # would BLOCK the promotion on an ask that never left.
    data = json.dumps(signed_control_event(settings, json.loads(event.model_dump_json())))

    async def _publish() -> None:
        async with DaprClient() as client:
            await publish_event(
                client,
                timeout_seconds=settings.publish_timeout_seconds,
                pubsub_name=settings.pubsub,
                topic_name=CONTROL_TOPIC,
                data=data,
                data_content_type="application/json",
            )

    try:
        _run_async(_publish())
    except Exception:
        log.exception("medallion_promotion_ask_failed", extra={"dataset": spec.to_dataset, "token": spec.token})
        return False
    log.info("medallion_promotion_review_requested", extra={"dataset": spec.to_dataset, "approver": spec.approver})
    return True


def _resume_publish(
    *,
    catalog_url: str,
    table_id: str,
    version: int,
    key_column: str,
    accept_assertions: list[str],
    identity_token_file: str,
    timeout_seconds: float,
    originator: str = "",
) -> None:
    """The tag move an approval resumes with. A seam so the activity is testable without a catalog.

    ``originator`` is the person whose batch this is, not the approver: this publish advances the tag,
    which wakes the NEXT tier through ``table_published``, and the producer authenticates as itself —
    so without it every failure below this promotion addresses a service.
    """
    from medallion.services.catalog_register import publish_stage_output

    publish_stage_output(
        catalog_url=catalog_url,
        table_id=table_id,
        version=version,
        key_column=key_column,
        accept_assertions=accept_assertions,
        identity_token_file=identity_token_file,
        timeout_seconds=timeout_seconds,
        originator=originator,
    )


def publish_promotion(ctx: WorkflowActivityContext, spec: PromotionSpec) -> None:
    """Resume the promotion a person approved, through the catalog — the only door that advances a tier.

    It MOVES THE TAG to the version the hold was taken on; the tag move is what wakes the next lane.
    It cannot be an ordinary re-publish: the version still fails the assertion it failed the first
    time, so the door would refuse it for exactly the reason the approver just overruled. It publishes
    with the findings the review ACCEPTED, which is the door built for this — named findings only, and
    structural ones never.
    """
    # Dapr hands an activity the DECODED DICT, not the annotated model: the input crossed the
    # durable boundary as JSON and the SDK never reads the annotation. Coerce before use, or every
    # attribute read below is an AttributeError the moment a real workflow runs it (measured live:
    # `'dict' object has no attribute 'outcome'` killed the cascade's own failure reporter).
    spec = PromotionSpec.model_validate(spec)
    from medallion.core.config import get_settings

    settings = get_settings()
    _resume_publish(
        catalog_url=settings.catalog_url,
        table_id=spec.to_dataset,
        version=spec.version,
        key_column=settings.quality_key_column,
        accept_assertions=list(spec.reasons),
        identity_token_file=settings.catalog_identity_token_file,
        timeout_seconds=settings.publish_timeout_seconds,
        originator=spec.originator,
    )
    log.info("medallion_promotion_published", extra={"dataset": spec.to_dataset, "version": spec.version, "accepted": spec.reasons})


def emit_promotion_outcome(ctx: WorkflowActivityContext, payload: PromotionReport) -> None:
    """Record the decision where the AUDIT lives.

    Workflow history is retention-bounded (7d COMPLETED / 30d FAILED), so it is a cache; lineage is
    the durable record. A decision surviving only in history is one the estate forgets.
    """
    # Dapr hands an activity the DECODED DICT, not the annotated model: the input crossed the
    # durable boundary as JSON and the SDK never reads the annotation. Coerce before use, or every
    # attribute read below is an AttributeError the moment a real workflow runs it (measured live:
    # `'dict' object has no attribute 'outcome'` killed the cascade's own failure reporter).
    payload = PromotionReport.model_validate(payload)
    from dapr.aio.clients import DaprClient

    from medallion.core.config import get_settings
    from medallion.schemas.events import build_run_event

    spec = payload.spec
    outcome = payload.outcome
    settings = get_settings()
    # A KEY MISS RAISES, before anything is counted or sent ([[XC-078]]). Nothing leaves unsigned, so the best-effort
    # publish below would drop the record on the first attempt, where the raise hands it to `SIGNING_ACTIVITY_RETRY`, and
    # a count taken first would be taken again on every attempt.
    require_signing_key(settings)
    # PROMOTED | REJECTED | BLOCKED | EXPIRED | PROMOTION_FAILED — a closed set decided by the review
    # body, never caller input. PROMOTION_FAILED means the person APPROVED and the publish was
    # refused: the decision is real, the tag did not move, and only a distinct status can say both.
    record_promotion_outcome(outcome.status)
    approved = outcome.status == "PROMOTED"
    event = build_run_event(
        # THE STAGE'S identity, off the spec: `settings` here is the PRODUCER's, which describes no stage.
        operation=spec.operation,
        author=spec.author,
        author_subject=settings.fga_service_identity,
        job_namespace=settings.job_namespace,
        # The nodes the held stage wrote its own lineage on, exactly as it resolved them.
        inputs=[(spec.from_namespace, spec.from_dataset)],
        output_namespace=spec.to_namespace,
        output_name=spec.to_dataset,
        # The version the approver ruled on; `build_run_event`'s default of 1 would name a version the
        # table may never have held, in the record that is supposed to be the durable one.
        version=spec.version,
        token=f"{spec.token}:promotion-{outcome.status.lower()}",
        project=spec.project or None,
        originator=spec.originator or None,
        event_type="COMPLETE" if approved else "FAIL",
        error_message=None if approved else f"promotion {outcome.status.lower().replace('promotion_', '')}: {', '.join(outcome.reasons) or 'quality review'}",
    )
    lance = event["run"]["facets"].setdefault("lance", {})
    lance["promotion_status"] = outcome.status
    if outcome.decided_by:
        lance["promotion_decided_by"] = outcome.decided_by

    async def _publish() -> None:
        async with DaprClient() as client:
            await emit_lineage(client, settings, event)

    # emit_promotion_outcome logged NOTHING at all on this path, while its docstring calls workflow
    # history "a cache; lineage is the durable record" — a dropped publish silently emptied the record.
    # NAMED, like its three siblings in this file. When this publish fails, the log line is the only
    # thing left of a decision a person made — `emit_promotion_outcome` is the sole writer of the
    # durable record, and its own docstring calls workflow history "a cache". An unnamed line cannot
    # be tied back to a promotion, so the audit was lost twice: once in lineage, once in the log.
    with best_effort(
        "promotion_outcome",
        token=spec.token,
        dataset=spec.to_dataset,
        status=outcome.status,
        decided_by=outcome.decided_by,
    ):
        _run_async(_publish())


# The registration tuples live at the END of the module, after every symbol they name. They used to
# sit mid-file, which worked only for as long as nothing was defined below them — adding the train
# watcher turned that into a NameError at import, i.e. a service that cannot start.
WORKFLOWS = (promotion_review,)

ACTIVITIES = (
    resolve_review_policy,
    request_approval,
    publish_promotion,
    emit_promotion_outcome,
)
