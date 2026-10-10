"""The CONTROL lane's relay — the cron tick that re-publishes staged ``catalog.control.v1`` events.

``DaprControlEmitter.emit`` stages every control event to ``LANCE_CONTROL_OUTBOX_URI`` before it
publishes and drops it only on ack, so a NATS blip leaves the event durable rather than gone. This is
the other half: the thing that reads that prefix and delivers what it finds.

**Why it matters more than "a refresh hint".** Most control events are exactly that — a console ring
buffer or a tag-polling reader loses a redraw. ``table_published`` is not: the stage runner does not fire the
next stage's topic, and ``/publication-arrival`` receiving this event is the ONLY thing that WAKES
silver->gold. The medallion's cascade-lag cron re-reads the ``published`` tag since
`docs/adr/0051-cascade-repair-detection-and-the-repair-verb-2026-09-04.md "Cascade repair"` C3, and that does not weaken this argument by a word: it MEASURES how far a
tier has fallen behind and advances nothing, so a lost publish is still a cascade that stops. A dropped one ends the cascade with the tag advanced, the data consumable, the
route 200, every pod green, and nothing red.

**IT DELIVERS ONLY WHAT THIS CATALOG SIGNED, AND SIGNS NOTHING** ([[XC-078]]). A staged object proves nothing about who
wrote it: anything that can write under the prefix can stage an object that parses as a control event. A relay that signed
what it found would sign that object as the catalog, declaring whatever person it named as the one the catalog
authenticated, and every enforcing door would admit it. So the catalog's emitter signs each event before it stages it and
withholds what it cannot sign (`service_kit.control_emit`), and the relay verifies each staged object as a control event
signed by THIS catalog's own identity, against the keys that identity publishes, and republishes the staged bytes as they
are:

* verified: republished verbatim, then dropped on ack;
* not a control event, or not signed by this catalog (unsigned, signed by another identity, altered, an unknown key, a
  bad encoding): POISON, retired unpublished and counted, and the pass goes on;
* the published keys cannot be read: no verdict on any signature, so the pass stops and everything stays staged.

A catalog that does not sign has no key to verify against and adds nothing to what it forwards: it delivers what parses,
which is no more than any publisher on the topic could put there itself.

**WHY HERE, IN THE CATALOG.** The lineage relay lives in the lineage service because its drain is an
INGEST — it writes each recovered event into the AGE graph and the durable feed, work only that service
can do. A control event has no graph to re-ingest into; the only thing owed to it is delivery onto the
topic it never reached. That makes the producer the right host: the catalog already owns the prefix
(``LANCE_CONTROL_OUTBOX_URI``), the component name (``LANCE_CONTROL_PUBSUB``), the S3 credentials that
address the prefix, and a Dapr sidecar. Any other service would have to be taught all four, and a
second copy of "which prefix does the control lane stage to" is how the two ends drift apart.

**WHY A CRON BINDING, NOT THE JOBS API.** ``.claude/skills/rask-dapr`` records the 2026-08-28 ruling:
recurring scan-and-converge is a cron binding, and the Jobs API is refused for it. This is convergence
work by the same test — level-triggered, idempotent, self-healing, late-is-free. The schedule is
Component config in the chart, so this module keeps no scheduler thread and nothing to drain at
shutdown.

**Delivered at the POD ROOT.** A Dapr input binding arrives as ``POST /<component name>``, never under a
prefix, and this app's routers already mount at the root (``service_kit.lance_app``). The Component's
name, ``LANCE_CONTROL_RELAY_BINDING_NAME`` and the served path are ONE string — see
:func:`build_control_relay_router`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ValidationError

from catalog.api.dependencies import SettingsDep
from catalog.core.config import Settings
from lineage_kit import SigningKey
from lineage_kit.keys import PublishedKeys
from lineage_kit.signing import KeySourceUnavailableError, SignatureError, VerifiedSignature, control_signature_of, verify_control_signature
from service_kit import dapr_publish
from service_kit.control_events import CONTROL_TOPIC, CatalogControlEvent
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.governed.signing_key import SIGNING_STATE, SigningKeyHolder, published_key_fetch
from service_kit.lakehouse import outbox, outbox_metrics


log = logging.getLogger(__name__)

#: Checks one staged control event as THIS catalog's: answers what its signature established, and raises
#: `SignatureError` for an event this catalog did not sign and `KeySourceUnavailableError` while the keys it publishes
#: cannot be read. Blocking, since a key read is a call to the sidecar.
type StagedVerifier = Callable[[Mapping[str, Any]], VerifiedSignature]

#: Bounds what a staged object's own text puts on a log line: its key and its action are whatever its writer chose.
_MAX_LOGGED_CHARS: Final = 300

#: How many staged events one tick drains. The lineage drain learned this the hard way (docs/adr/0004-p1-2-bounded-oldest-first-outbox-drain.md
#: P1.2): materialising a whole prefix inside the tick makes the relay fail hardest exactly when a
#: backlog exists, which is the only situation it is for. The remainder drains next tick, oldest-first,
#: so nothing starves. A literal rather than a knob because the control lane's event rate is bounded by
#: catalog MUTATIONS, not by row counts — there is no deployment shape that wants a different number,
#: and an unused setting is one more thing the chart can render wrong.
DRAIN_LIMIT = 500

#: Single-flight for THIS PROCESS. Dapr's cron binding has no overlap protection, so a tick that
#: outruns its period is delivered anyway and two passes would list the same objects.
#:
#: Deliberately NOT a cluster-wide lock, and the catalog may run several replicas. Two replicas racing
#: this prefix costs a duplicate PUBLISH, never a lost or corrupted event: `list_events` skips an object
#: another drain has already removed, and every consumer of this topic is built for at-least-once —
#: the ring buffer dedupes on `event_id`, and the cascade's deterministic instance id dedupes the work
#: (see `_republish`). Buying cross-replica exclusion would mean a distributed lock component, which the
#: estate has an open ruling against adopting while its API is Alpha.
_relay_lock = asyncio.Lock()


class ControlRelayReport(BaseModel):
    """One tick's findings — the response body and the shape its log line counts."""

    #: Staged events found under the prefix at the START of the tick (the saturation snapshot).
    depth: int = 0
    #: Age of the oldest staged event. Bounds how long the control lane has been undelivered.
    oldest_age_seconds: float = 0.0
    #: Events re-published onto the control topic and then dropped.
    republished: int = 0
    #: Objects retired unpublished so they cannot wedge the drain: one that is not a control event, or, on a catalog that
    #: signs, one this catalog did not sign. Non-zero is a producer bug or a forgery.
    poison_dropped: int = 0
    #: The pass stopped because the keys this catalog publishes could not be read, or a key id could not be decided yet:
    #: everything still staged waits, because the relay publishes only what it verified.
    keys_unavailable: bool = False
    #: A tick that found another pass already running on this replica.
    skipped: bool = False
    reason: str = ""


def get_control_publisher(request: Request) -> object | None:
    """The lifespan-built Dapr client, or ``None`` when this deployment has no sidecar transport.

    Read off ``app.state`` rather than constructed here: a per-tick client would open and discard a
    gRPC channel every cron interval, and a deployment with control emission off has no client to build.
    Typed ``object`` because concrete Dapr clients differ in signature and this module only forwards one
    to ``dapr_publish``. ``None`` makes the tick a pure OBSERVATION — it still reports depth and age, so
    a backlog accruing on a mis-wired deployment is visible instead of silently unmeasured.
    """
    return getattr(request.app.state, "dapr_client", None)


ControlPublisherDep = Annotated[object | None, Depends(get_control_publisher)]


def _published_keys(request: Request, settings: Settings) -> PublishedKeys:
    """This app's key reader, built on first use and kept on the app so its cache outlives one tick, as the doors keep theirs."""
    state = request.app.state
    keys: PublishedKeys | None = getattr(state, "published_keys", None)
    if keys is None:
        keys = state.published_keys = PublishedKeys(published_key_fetch(settings.dapr_secret_store))
    return keys


async def get_staged_verifier(request: Request, settings: SettingsDep) -> StagedVerifier | None:
    """The check a staged object passes before it goes out, or ``None`` for a catalog that does not sign.

    Signed by THIS catalog and nobody else: the identity of the holder the lifespan attached is the only signer and the
    only delegator, so a person actor verifies only under the delegation this catalog declares for the person it
    authenticated. Checked against the keys that identity publishes, through this pod's sidecar and the catalog's secret
    store. Whether the holder has resolved its own private key does not matter here, since the relay signs nothing.

    `async def` although it awaits nothing, so the key reader is built on the event loop and two first ticks share one.
    """
    holder: SigningKeyHolder[SigningKey] | None = getattr(request.app.state, SIGNING_STATE, None)
    if holder is None:
        return None
    keys = _published_keys(request, settings)
    own = frozenset({holder.identity})

    def verify(envelope: Mapping[str, Any]) -> VerifiedSignature:
        claim = control_signature_of(envelope)
        return verify_control_signature(envelope, source=keys.for_event(claim.kid if claim else None), signers=own, delegators=own)

    return verify


StagedVerifierDep = Annotated[StagedVerifier | None, Depends(get_staged_verifier)]


def _bounded(text: str) -> str:
    return text if len(text) <= _MAX_LOGGED_CHARS else f"{text[:_MAX_LOGGED_CHARS]}..."


def _action_of(event_json: str) -> str | None:
    """The action a staged object names, for its log line, or None when it names none that can be read.

    Read off the RAW text, because the object is poison exactly when the model may not parse it. Total over whatever
    was staged: a document nested past the interpreter's recursion limit, or one that is not an object, must not raise
    here, since this runs where the object is retired and a raise would leave it to wedge every tick.
    """
    try:
        parsed = json.loads(event_json)
    except (ValueError, RecursionError):
        return None
    action = parsed.get("action") if isinstance(parsed, dict) else None
    return _bounded(action) if isinstance(action, str) and action else None


async def _republish(publisher: object, settings: Settings, event_json: str) -> None:
    """Deliver ONE staged event onto the control topic, as the staged text.

    Never ``event.model_dump_json()``. Round-tripping through the model re-serializes ``occurred_at``,
    re-orders ``extra`` and drops the ``rask_signature`` the model ignores, and the point of the
    redelivery is that subscribers see exactly what they would have seen the first time.

    The staged ``event_id`` is also what makes the redelivery IDEMPOTENT, and the chain is worth stating
    because it is the whole answer to "does this drive the cascade twice?":

    ``event_id`` survives -> ``/publication-arrival`` mints its stage ``token`` from it
    (`publication_trigger.py`) -> ``derive_idempotency_key(stage, token, from_uri, to_uri, code_version)`` hashes that
    into the run's deterministic key, its plan's action id (`medallion/services/stage_submit.py`) -> the plan store
    answers the plan already written under that key, and the engine re-attaches to the job submitted under it. So a
    duplicate delivery attaches to the run already in flight rather than starting a second.

    Every other subscriber on this topic keys on the same id: the catalog's own ring buffer dedupes on
    ``event_id``, and the notifications plane's ledger on ``<event_id>@<ACTION>``.

    THE BOUND OF THAT GUARANTEE, stated rather than assumed: the engine dedupes against instances it
    still HOLDS. A redelivery arriving after the original instance has completed and been purged
    schedules a fresh one, which re-runs the same hop. That is duplicate COMPUTE, not duplicate data —
    the stage write is single-flighted and content-deterministic, so the second pass is a same-bytes
    overwrite (`bronze_arrival.py` records the same property for the two cascade heads).
    """
    await dapr_publish.publish_event(  # noqa: TID251
        publisher,
        timeout_seconds=settings.control_emit_timeout_seconds,
        pubsub_name=settings.control_pubsub,
        topic_name=CONTROL_TOPIC,
        data=event_json,
        data_content_type="application/json",
    )


async def _drain(settings: Settings, publisher: object | None, verify: StagedVerifier | None) -> ControlRelayReport:
    """Re-publish and drop every staged control event this catalog may deliver, oldest first, up to :data:`DRAIN_LIMIT`.

    PUBLISH BEFORE DROP, never the other way round: a publish that fails must leave the object for the
    next tick, which is the entire point of staging. A redelivery costs a duplicate the lane already
    tolerates; a premature drop costs the cascade.

    VERIFIED BEFORE IT GOES when this catalog signs (``verify``), and never signed here: the staged text
    goes out as it is or not at all. An object that is not a control event, or that this catalog did not
    sign, is retired as poison and never published. Keys that cannot be read stop the pass with every
    object still staged, because they say nothing about any signature.

    Blocking object-store IO and the key reads run in the threadpool so a slow prefix never stalls the event loop.
    """
    options = settings.storage_options()
    depth, oldest_age = await run_in_threadpool(outbox.backlog, settings.control_outbox_uri, options)
    # ONE instrument for both lanes, on purpose. `chart/alerting/rules.yml` alerts on
    # `max(outbox_depth) > 0` and `max(outbox_oldest_age_seconds) > 300` — lane-agnostic by
    # construction — so observing here makes a stuck CONTROL outbox pageable with no new rule, and the
    # OTel resource's `service.name` (catalog vs lineage) says which lane is stuck. The alternative,
    # staying silent to keep the lineage series pure, is exactly the invisible-failure shape this whole
    # relay exists to remove.
    outbox_metrics.observe_backlog(depth, oldest_age)
    report = ControlRelayReport(depth=depth, oldest_age_seconds=round(oldest_age, 1))
    if not depth:
        return report
    if publisher is None:
        # No transport, so nothing here can be delivered — and NOTHING is dropped, because the staged
        # objects are the only copies. Reported rather than silent: a rendered outbox with no sidecar
        # client is a misconfiguration whose only symptom is a backlog that never falls.
        log.warning("control_relay_no_publisher", extra={"depth": depth, "oldest_age_seconds": report.oldest_age_seconds})
        return report

    staged = await run_in_threadpool(lambda: list(outbox.list_events(settings.control_outbox_uri, options, limit=DRAIN_LIMIT)))
    for key, event_json in staged:
        try:
            # VALIDATE, THEN VERIFY; the parsed model is deliberately discarded. Validation asks "could a
            # subscriber read this?" and verification "did this catalog write it?", and the answers decide
            # poison vs relay. What goes on the wire is the staged text, never a re-serialization of what was
            # parsed here, and never a signature made here.
            CatalogControlEvent.model_validate_json(event_json)
            if verify is not None:
                await run_in_threadpool(verify, json.loads(event_json))
        except KeySourceUnavailableError as exc:
            # No verdict on any signature: nothing may go out and nothing may be retired. Stop the pass like
            # a bus still down, and every object stays staged for the next tick to retry from the oldest.
            log.warning("control_relay_keys_unavailable", extra={"key": _bounded(key), "depth": depth, "error": _bounded(str(exc))})
            report.keys_unavailable = True
            break
        except (ValidationError, SignatureError) as exc:
            # NARROW on purpose: only an object no subscriber can read, or one this catalog did not sign, is
            # poison. A broad `except` here would retire a staged event on any transient failure, i.e. destroy
            # the one durable copy this module exists to deliver.
            reason = exc.reason if isinstance(exc, SignatureError) else "invalid"
            log.warning("control_outbox_unverifiable", extra={"key": _bounded(key), "action": _action_of(event_json), "reason": reason})
            outbox_metrics.record_poison_dropped()
            await run_in_threadpool(outbox.drop_event, settings.control_outbox_uri, options, key)
            report.poison_dropped += 1
            continue
        try:
            await _republish(publisher, settings, event_json)
        except Exception as exc:
            # The bus is still down. Stop the pass rather than grinding the whole backlog against it:
            # every remaining object stays staged and the next tick retries from the oldest.
            log.warning("control_outbox_republish_failed", extra={"key": key, "error": str(exc)})
            outbox_metrics.record_publish_failed()
            break
        await run_in_threadpool(outbox.drop_event, settings.control_outbox_uri, options, key)
        report.republished += 1
    outbox_metrics.record_drained(report.republished)
    return report


async def _on_cron(
    settings: SettingsDep,
    publisher: ControlPublisherDep,
    verify: StagedVerifierDep,
    _: Annotated[None, Depends(require_dapr_token)],
) -> ControlRelayReport:
    """One relay tick, driven by the Dapr cron binding.

    Guarded by ``require_dapr_token`` so only the sidecar's cron may drive it: an unauthenticated door
    here would let anything that can reach the port replay every staged control event on demand.
    """
    if _relay_lock.locked():
        log.info("control_relay_skipped_overlap")
        return ControlRelayReport(skipped=True, reason="another control relay pass is in progress")
    async with _relay_lock:
        report = await _drain(settings, publisher, verify)
    if report.poison_dropped:
        log.warning("control_outbox_poison", extra={"dropped": report.poison_dropped})
    log.info(
        "control_relay_tick",
        extra={
            "depth": report.depth,
            "oldest_age_seconds": report.oldest_age_seconds,
            "republished": report.republished,
            "poison_dropped": report.poison_dropped,
            "keys_unavailable": report.keys_unavailable,
        },
    )
    return report


async def _ack_binding() -> dict[str, str]:
    """Dapr's startup pre-flight (``OPTIONS /<binding name>``) — a 2xx confirms this app consumes the
    binding. Without it the app answers 405, the sidecar logs the binding as not consumed, and the
    schedule ticks into nothing."""
    return {"status": "ok"}


def build_control_relay_router(binding_name: str) -> APIRouter:
    """Register the relay at the EXACT binding name the sidecar delivers to (POST drain, OPTIONS ack)."""
    router = APIRouter()
    router.add_api_route(f"/{binding_name}", _on_cron, methods=["POST"], tags=["control-relay"])
    router.add_api_route(f"/{binding_name}", _ack_binding, methods=["OPTIONS"], include_in_schema=False)
    return router


def mount_control_relay(app: FastAPI, binding_name: str) -> bool:
    """Mount the relay when a binding name is configured; report whether it was.

    Opt-in like lineage's reconcile route: an unnamed binding means a deployment that stages nothing
    (or has no sidecar), and mounting an always-live cron door for it would add a replay surface with
    no component behind it.
    """
    if not binding_name:
        return False
    app.include_router(build_control_relay_router(binding_name))
    return True
