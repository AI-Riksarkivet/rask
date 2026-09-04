"""The cascade's re-run verb: re-drive ONE missed hop — docs/DECISIONS.md "Cascade repair" (C2).

A hop can be missed four ways (that file's L1..L4) and the estate could DETECT some of them and
REPAIR none: the only remedy was re-publishing the upstream table, which re-drives every consumer of
it rather than the one edge that failed. This is the edge-addressed remedy.

**IT LIVES ON THE PRODUCER, and mints the trigger itself.** The first draft had it forward to the
mover, exactly as `terminate` does, for one reason: a Ray-liveness check needs `to_uri` and
`MEDALLION_RAY_CODE_VERSION`, which only the mover holds. That check is gone (R2a), so the reason is
gone with it — and the producer already mints stage triggers, in `publication_trigger`'s
`table_published` subscription. `build_stage_trigger` was written for exactly two callers and this is
the second; a hand-maintained second copy of that shape is what its docstring forbids.

**THE TOKEN IS OPTIONAL, and supplying it is what makes the cheap repair reachable (R1).** It is the
`table_published` event's `event_id`, which the control outbox drops on ack and no durable store
retains — so this verb cannot re-mint one, and a design that required it would be unimplementable.

* **supplied** (an operator has it from the DLQ line, which carries it) — the trigger is verbatim, so
  the mover derives the same deterministic instance id and `submit_or_reattach` REATTACHES to a
  running or succeeded job instead of starting a second. This is the ideal repair, and it costs no
  extra call: a duplicate id whose job is RUNNING already answers `"reattached"`;
* **absent** — a fresh token, a full recompute, a second lineage run node. Honest, and the common case
  for the never-ran shape where nothing exists to reattach to.

**No 409, and that is a decision rather than an omission (R2a).** The check R2 prescribed needs a Ray
job LISTING, and Ray's `GET /api/jobs/` accepts no parameters at all — measured on this estate at
81,155 jobs / 164.7 MB in one response, 1179 MiB RSS against a 1536 MiB limit. Putting that in a
request handler is the pattern this estate has just spent a release removing. The stage write is
`mode="overwrite"`, a Lance commit that `bronze_arrival` calls overwrite-convergent, so a racing
fresh-token re-run reaches a correct final state and wastes only compute. The response SAYS SO
rather than implying a guarantee the listing could not make.

**The rung is the MOVER's own (R4).** Not `/produce`'s `can_administer`, which `promotions.py` records
as "coarser AND different, and would lock out exactly the non-admin validator the rung exists for".
A silver->gold re-run asks `can_promote` on `namespace:<project>-gold`, exactly as the mover asks when
it runs the hop itself. Its sibling `terminate` sits on `authorize_produce` — two verbs on this plane,
two rungs — which is defensible because stopping is not re-driving, and is written down rather than
discovered.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from lance_namespace import PermissionDeniedError, ServiceUnavailableError
from pydantic import BaseModel, ConfigDict, Field

from medallion.api.dependencies import FgaClientDep, SettingsDep
from medallion.api.produce_auth import authenticate_subject
from medallion.core.config import MedallionSettings, MoverGate
from medallion.services.publication_trigger import build_stage_trigger
from service_kit import dapr_publish
from service_kit.draining import refuse_when_draining
from service_kit.governed import fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, audit


log = logging.getLogger(__name__)

router = APIRouter(tags=["movers"])

#: What the verb predicts it will do, never what it observed. The path is decided inside
#: `submit_stage` AFTER this returns (R3), and two further horizons make even the prediction
#: conditional: Ray's GCS is not fault-tolerant here, so a head restart loses the SUCCEEDED job a
#: reattach would have found; and the Ray submission id folds `code_version` while the workflow id
#: does not, so a same-token re-run AFTER A DEPLOY is a full recompute regardless.
REATTACH = "reattach-if-live"
RECOMPUTE = "recompute"


class RerunRequest(BaseModel):
    """One edge, one range. The fields an operator has in front of them from a DLQ line."""

    model_config = ConfigDict(extra="forbid")

    #: The PUBLISHED table, as the control event named it — `table:acme-silver$features` or the bare
    #: identifier. Its namespace IS the edge's source, which is why the verb needs no mover name.
    object_id: str = Field(min_length=1)
    project: str = Field(min_length=1)
    #: The delta this hop should consume. `from_version` absent means "everything up to `to_version`",
    #: carried as-is rather than coerced to 0 — "no prior publication" and "published from version 0"
    #: are different claims, and the mover reads them differently.
    to_version: int
    from_version: int | None = None
    #: The original `table_published` event id. Supplied → the cheap repair; absent → a full
    #: recompute. See the module docstring; this is the whole of R1.
    token: str | None = None
    #: Carried through so a re-run of a person's cascade still reaches that person's inbox. The
    #: notifications plane re-derives visibility per recipient, so this authorizes nothing.
    originator: str | None = None
    cascade_id: str | None = None


class RerunAccepted(BaseModel):
    """What was re-driven, and what it will PROBABLY cost."""

    model_config = ConfigDict(extra="forbid")

    dataset: str
    namespace: str
    topic: str
    token: str
    #: `reattach-if-live` or `recompute` — PREDICTED (R3). Never the path taken.
    mode: str
    #: Said out loud on the recompute path: a fresh token yields a different submission id, so this
    #: may duplicate a live job's work. The write is overwrite-convergent, so the DATA is safe.
    note: str = ""


async def _require_edge_rung(fga_client: Any, *, subject: str, project: str, gate: MoverGate) -> None:  # noqa: ANN401 — OpenFgaClient
    """The mover's own rung on the tier this edge writes, audited like every other authz decision.

    Fails CLOSED on an authz outage, and audits that separately from a denial: "we could not ask" and
    "we asked and the answer was no" are different operator problems, and collapsing them is how an
    FGA blip reads as a permissions bug.
    """
    obj = f"namespace:{project}-{gate.to_namespace}"
    try:
        allowed = await fga.check(fga_client, user=subject, relation=gate.required_action, obj=obj)
    except ServiceUnavailableError:
        audit(gate.required_action, FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
        raise ServiceUnavailableError("authorization service is not available") from None
    audit(gate.required_action, ALLOW if allowed else DENY, subject=subject, resource=obj)
    if not allowed:
        raise PermissionDeniedError(f"re-running this edge needs {gate.required_action} on {obj}")


def _edge(settings: MedallionSettings, namespace: str) -> tuple[MoverGate, str]:
    """This edge's gate and its trigger topic, or a refusal naming what IS configured.

    BOTH are resolved before anything is published, and both are keyed on the same source namespace —
    a deployment that declared one and not the other would either publish an unauthorized trigger or
    authorize one that goes nowhere.
    """
    gate = (settings.mover_gates or {}).get(namespace)
    topic = (settings.transform_routes or {}).get(namespace)
    if gate is None or not topic:
        known = sorted(set(settings.mover_gates or {}) & set(settings.transform_routes or {}))
        raise PermissionDeniedError(f"{namespace!r} is not a cascade edge this deployment drives; edges: {known}")
    return gate, topic


# B6: a draining pod must not START work. The re-run publishes a trigger that a mover then executes,
# so what is at risk is not the mover's work but this publish — the sidecar goes down with the pod, and
# a trigger lost mid-flight is a repair an operator believes happened. 503 + Retry-After, exactly as
# `/produce` and `/train` answer, rather than a 202 for a hop nothing will run.
@router.post("/movers/stages/rerun", status_code=202, dependencies=[Depends(refuse_when_draining)])
async def rerun_stage(
    body: RerunRequest,
    request: Request,
    settings: SettingsDep,
    fga_client: FgaClientDep,
    subject: Annotated[str | None, Depends(authenticate_subject)],
) -> RerunAccepted:
    """Re-drive one cascade edge. 202, because the hop runs on the bus like every other one.

    `authenticate_subject` rather than `authorize_produce`, because the rung is the mover's (R4) and
    the produce gate would fuse the two into "admin AND validator" — locking out the validator the
    rung exists for. There is NO dev-open path here for the same reason the promotion decision has
    none: a re-run is an act with a responsible party, and an anonymous one is not an act anyone made.
    """
    if not subject:
        raise PermissionDeniedError("re-running a cascade edge needs a signed-in caller")
    token = body.token or uuid4().hex
    extra: dict[str, Any] = {"project": body.project, "from_version": body.from_version, "to_version": body.to_version}
    if body.originator:
        extra["originator"] = body.originator
    if body.cascade_id:
        extra["cascade_id"] = body.cascade_id
    # MINTED BEFORE THE GATE IS RESOLVED, because the trigger is what tells us which edge this is:
    # `build_stage_trigger` owns the object_id -> (namespace, dataset) rule, and re-deriving it here
    # to authorize would be the second hand-maintained copy its docstring forbids.
    trigger = build_stage_trigger(object_id=body.object_id, event_id=token, extra=extra)
    if trigger is None:
        # 403, not 404, and deliberately: the object may exist perfectly well and simply not name a
        # cascade lane. `PermissionDeniedError` is what this plane's doors raise for "not yours to
        # drive"; a 404 would tell an unauthorized caller which object ids are real.
        raise PermissionDeniedError(f"{body.object_id!r} does not name a cascade edge")
    namespace = str(trigger["namespace"])
    gate, topic = _edge(settings, namespace)
    if fga_client is None:
        raise ServiceUnavailableError("authorization service is not available")
    await _require_edge_rung(fga_client, subject=subject, project=body.project, gate=gate)

    landed = await dapr_publish.publish_json(
        request.app.state.dapr,
        pubsub_name=settings.pubsub,
        topic_name=topic,
        payload=trigger,
        timeout_seconds=settings.publish_timeout_seconds,
        failure_event="medallion_rerun_trigger_failed",
        context={"token": token, "object_id": body.object_id},
    )
    if not landed:
        # The broker is the one failure a caller can act on by retrying, so it is a 503 rather than a
        # 202 that re-drove nothing. Every other refusal above happened before anything was published.
        raise ServiceUnavailableError("the re-run trigger could not be published")
    reattaching = body.token is not None
    log.info(
        "medallion_stage_rerun",
        extra={"dataset": trigger["dataset"], "namespace": namespace, "subject": subject, "mode": REATTACH if reattaching else RECOMPUTE},
    )
    return RerunAccepted(
        dataset=str(trigger["dataset"]),
        namespace=namespace,
        topic=topic,
        token=token,
        mode=REATTACH if reattaching else RECOMPUTE,
        note=""
        if reattaching
        else "no token supplied, so this mints a fresh one: it may duplicate a live job's work. The stage write is "
        "overwrite-convergent, so the data is safe; the cost is compute.",
    )
