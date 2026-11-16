"""The CONTROL lane's relay — the tick that re-publishes staged ``catalog.control.v1`` events.

``BusControlEmitter.emit`` stages every control event to ``LANCE_CONTROL_OUTBOX_URI`` before it
publishes and drops it only on ack, so a NATS blip leaves the event durable rather than gone. This is
the other half: the thing that reads that prefix and delivers what it finds.

**Why it matters more than "a refresh hint".** Most control events are exactly that — a console ring
buffer or a tag-polling reader loses a redraw. Not all of them: a notifier targets people off this
lane, so a dropped event is a person never told, with the route 200, every pod green, and nothing red.

**IT DELIVERS ONLY WHAT THIS CATALOG SIGNED, AND SIGNS NOTHING** ([[XC-078]]). A staged object proves nothing about who
wrote it: anything that can write under the prefix can stage an object that parses as a control event. A relay that signed
what it found would sign that object as the catalog, declaring whatever person it named as the one the catalog
authenticated, and every enforcing door would admit it. So the catalog's emitter signs each event before it stages it and
withholds what it cannot sign (`lakehouse.control_emit`), and the relay verifies each staged object as a control event
signed by THIS catalog's own identity, against the keys that identity publishes, and republishes the staged event as it
is, in the envelope derived from it (`lakehouse.cloud_events.control_envelope`):

* verified: republished as staged, then dropped on ack;
* not a control event, or not signed by this catalog (unsigned, signed by another identity, altered, an unknown key, a
  bad encoding), or one the bus can never carry (the publish seam's own refusal, before any I/O): POISON, retired
  unpublished and counted, and the pass goes on;
* the published keys cannot be read: no verdict on any signature, so the pass stops and everything stays staged.

A catalog that does not sign has no key to verify against and adds nothing to what it forwards: it delivers what parses,
which is no more than any publisher on the topic could put there itself.

**WHY HERE, IN THE CATALOG.** The lineage relay lives in the lineage service because its drain is an
INGEST — it records each recovered event in the commit feed and the edge list, work only that service
can do. A control event has no feed to re-ingest into; the only thing owed to it is delivery onto the
topic it never reached. That makes the producer the right host: the catalog already owns the prefix
(``LANCE_CONTROL_OUTBOX_URI``), the S3 credentials that address the prefix, and the bus user that may
publish the topic. Any other service would have to be given all three, and a
second copy of "which prefix does the control lane stage to" is how the two ends drift apart.

**ON THE CATALOG'S OWN TIMER** (`catalog.api.timers`), never a schedule on the bus: the relay exists for the bus
being away, so its tick must depend on nothing but the store the events are staged on. It is convergence work,
level-triggered, idempotent, self-healing and late-is-free, and it runs once a window whatever the replica count.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ValidationError

from catalog.api.ticks import Tick
from catalog.core.config import Settings
from lakehouse import outbox, outbox_metrics
from lakehouse.bus import Bus, publish_event  # noqa: TID251 — the control relay is a registered publish site
from lakehouse.cloud_events import CloudEvent, control_envelope
from lakehouse.control_events import CONTROL_TOPIC
from lakehouse.governed.signing_key import SIGNING_STATE, SigningKeyHolder, published_key_fetch
from lakehouse.lineage import SigningKey
from lakehouse.lineage.keys import PublishedKeys
from lakehouse.lineage.signing import KeySourceUnavailableError, SignatureError, VerifiedSignature, control_signature_of, verify_control_signature


log = logging.getLogger(__name__)

#: Checks one staged control event as THIS catalog's: answers what its signature established, and raises
#: `SignatureError` for an event this catalog did not sign and `KeySourceUnavailableError` while the keys it publishes
#: cannot be read. Blocking, since a key read is a call to the secret store.
type StagedVerifier = Callable[[Mapping[str, Any]], VerifiedSignature]

#: Bounds what an event's own text puts on a log line: a staged object's key and action, or a delivered event's claimed
#: identity and id, are whatever its writer chose.
_MAX_LOGGED_CHARS: Final = 300

#: How many staged events one tick drains. The lineage drain learned this the hard way (docs/adr/0004-p1-2-bounded-oldest-first-outbox-drain.md
#: P1.2): materialising a whole prefix inside the tick makes the relay fail hardest exactly when a
#: backlog exists, which is the only situation it is for. The remainder drains next tick, oldest-first,
#: so nothing starves. A literal rather than a knob because the control lane's event rate is bounded by
#: catalog MUTATIONS, not by row counts — there is no deployment shape that wants a different number,
#: and an unused setting is one more thing the chart can render wrong.
DRAIN_LIMIT = 500


class ControlRelayReport(BaseModel):
    """One tick's findings — the response body and the shape its log line counts."""

    #: Staged events found under the prefix at the START of the tick (the saturation snapshot).
    depth: int = 0
    #: Age of the oldest staged event. Bounds how long the control lane has been undelivered.
    oldest_age_seconds: float = 0.0
    #: Events re-published onto the control topic and then dropped.
    republished: int = 0
    #: Objects retired unpublished so they cannot wedge the drain: one that is not a control event, one the bus can never
    #: carry, or, on a catalog that signs, one this catalog did not sign. Non-zero is a producer bug or a forgery.
    poison_dropped: int = 0
    #: Objects whose delete the store refused, after their event was republished or they were judged poison. Each stays
    #: staged for the next tick and is counted as neither, and the pass goes on to the events behind it.
    left_staged: int = 0
    #: The pass stopped because the keys this catalog publishes could not be read, or a key id could not be decided yet:
    #: everything still staged waits, because the relay publishes only what it verified.
    keys_unavailable: bool = False


def get_control_publisher(request: Request) -> Bus | None:
    """This process's connection to the bus, or ``None`` when the chart names it no bus.

    Read off ``app.state``, where the lifespan keeps the one connection the process holds (`lakehouse.bus`).
    ``None`` makes the tick a pure OBSERVATION — it still reports depth and age, so
    a backlog accruing on a mis-wired deployment is visible instead of silently unmeasured.
    """
    return getattr(request.app.state, "bus", None)


def attach_published_keys(app: FastAPI) -> None:
    """Give ``app`` the one reader of its signers' published keys, which reads the secret store under this process's
    login when an event is verified. The catalog's start calls it, so each start reads through a cache of its own."""
    app.state.published_keys = PublishedKeys(published_key_fetch())


def published_keys(app: FastAPI) -> PublishedKeys:
    """This app's one key reader (:func:`attach_published_keys`), kept on the app so its cache outlives one delivery or
    tick.

    Shared by the relay and the control lane (`api/control_lane.py`): one reader per app, so both read the keys
    through one cache.
    """
    return app.state.published_keys


async def get_staged_verifier(request: Request) -> StagedVerifier | None:
    """The check a staged object passes before it goes out, or ``None`` for a catalog that does not sign.

    Signed by THIS catalog and nobody else: the identity of the holder the lifespan attached is the only signer and the
    only delegator, so a person actor verifies only under the delegation this catalog declares for the person it
    authenticated. Checked against the keys that identity publishes, read from the secret store under the catalog's own
    login. Whether the holder has resolved its own private key does not matter here, since the relay signs nothing.
    """
    holder: SigningKeyHolder[SigningKey] | None = getattr(request.app.state, SIGNING_STATE, None)
    if holder is None:
        return None
    keys = published_keys(request.app)
    own = frozenset({holder.identity})

    def verify(envelope: Mapping[str, Any]) -> VerifiedSignature:
        claim = control_signature_of(envelope)
        return verify_control_signature(envelope, source=keys.for_event(claim.kid if claim else None), signers=own, delegators=own)

    return verify


def bounded(text: str) -> str:
    """``text`` cut to what one log line carries of a writer's own words."""
    return text if len(text) <= _MAX_LOGGED_CHARS else f"{text[:_MAX_LOGGED_CHARS]}..."


def _action_of(event_json: str) -> str | None:
    """The action a staged object's event names, for its log line, or None when it names none that can be read.

    Read off the RAW text, because the object is poison exactly when the model may not parse it. Total over whatever
    was staged: a document nested past the interpreter's recursion limit, or one that is not an object, must not raise
    here, since this runs where the object is retired and a raise would leave it to wedge every tick.
    """
    try:
        parsed = json.loads(event_json)
    except (ValueError, RecursionError):
        return None
    event = parsed.get("data") if isinstance(parsed, dict) else None
    action = event.get("action") if isinstance(event, dict) else None
    return bounded(action) if isinstance(action, str) and action else None


async def _republish(publisher: Bus, settings: Settings, envelope: CloudEvent) -> None:
    """Deliver ONE staged event onto the control topic, in the envelope derived from the staged event.

    Never the envelope of ``event.model_dump()``. Round-tripping through the model re-serializes ``occurred_at``,
    fills the members the schema defaults and drops the ``rask_signature`` the model ignores, and the point of the
    redelivery is that subscribers see exactly what they would have seen the first time.

    The staged ``event_id`` is what makes the redelivery IDEMPOTENT, twice over. It is the envelope's ``id``, so the
    stream stores a copy of an event it already holds once, inside its duplicate window. And every subscriber on this
    topic keys on it: the catalog's own ring buffer dedupes on ``event_id``, and a notifier keys what it delivers on it
    too (a ledger on ``<event_id>@<ACTION>``).
    """
    await publish_event(publisher, envelope, topic=CONTROL_TOPIC, timeout_seconds=settings.control_emit_timeout_seconds)


async def _drain(settings: Settings, publisher: Bus | None, verify: StagedVerifier | None) -> ControlRelayReport:
    """Re-publish and drop every staged control event this catalog may deliver, oldest first, up to :data:`DRAIN_LIMIT`.

    PUBLISH BEFORE DROP, never the other way round: a publish that fails must leave the object for the
    next tick, which is the entire point of staging. A redelivery costs a duplicate the lane already
    tolerates; a premature drop costs the cascade.

    A DELETE THE STORE REFUSES IS THAT OBJECT'S OWN FAILURE. The object stays staged and the pass goes on: the next tick
    republishes it under the same ``event_id``, which the stream and every subscriber deduplicate on, and an object no
    tick can delete never holds the events behind it. Only a publish that fails stops the pass, since that is the bus.

    VERIFIED BEFORE IT GOES when this catalog signs (``verify``), and never signed here: the staged event
    goes out as it is or not at all. An object that is not a control event, or that this catalog did not
    sign, is retired as poison and never published. Keys that cannot be read stop the pass with every
    object still staged, because they say nothing about any signature.

    Blocking object-store IO and the key reads run in the threadpool so a slow prefix never stalls the event loop.
    """
    options = settings.storage_options()
    depth, oldest_age = await run_in_threadpool(outbox.backlog, settings.control_outbox_uri, options)
    # ONE instrument for both lanes, on purpose. `deploy/alerting/rules.yml` alerts on
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
        # objects are the only copies. Reported rather than silent: a rendered outbox in a process the chart
        # names no bus is a misconfiguration whose only symptom is a backlog that never falls.
        log.warning("control_relay_no_publisher", extra={"depth": depth, "oldest_age_seconds": report.oldest_age_seconds})
        return report

    async def drop_or_leave_staged(key: str) -> bool:
        """Delete one staged object and answer whether the store did; one it refuses stays staged, logged and counted on the report."""
        try:
            await run_in_threadpool(outbox.drop_event, settings.control_outbox_uri, options, key)
        except Exception as exc:
            log.warning("control_outbox_left_staged", extra={"key": bounded(key), "error": bounded(str(exc))})
            report.left_staged += 1
            return False
        return True

    async def retire(key: str) -> None:
        """Drop one staged object this lane can never deliver, so the pass goes on to the events behind it."""
        if await drop_or_leave_staged(key):
            outbox_metrics.record_poison_dropped()
            report.poison_dropped += 1

    staged = await run_in_threadpool(lambda: list(outbox.list_events(settings.control_outbox_uri, options, limit=DRAIN_LIMIT)))
    for key, event_json in staged:
        try:
            # ENVELOPED, THEN VERIFIED. The envelope is derived from the staged event, which must therefore be a
            # control event one can carry: that asks "could a subscriber read this?". Verification asks "did this
            # catalog write it?", and the answers decide poison vs relay. The envelope's data is the staged event as
            # it is, never a re-serialization of a parsed model, and never signed here; it goes out in the trace it
            # was staged under (ADR 0209). The staged object's refusal is a `ValidationError`, the poison verdict,
            # whatever is wrong with the text: not JSON, no envelope, or nested past the parser's limit.
            staged = outbox.staged_envelope(event_json)
            envelope = control_envelope(staged.data).with_trace_of(staged)
            if verify is not None:
                await run_in_threadpool(verify, envelope.data)
        except KeySourceUnavailableError as exc:
            # No verdict on any signature: nothing may go out and nothing may be retired. Stop the pass like
            # a bus still down, and every object stays staged for the next tick to retry from the oldest.
            log.warning("control_relay_keys_unavailable", extra={"key": bounded(key), "depth": depth, "error": bounded(str(exc))})
            report.keys_unavailable = True
            break
        except (ValidationError, SignatureError) as exc:
            # NARROW on purpose: only an object no subscriber can read, or one this catalog did not sign, is
            # poison. A broad `except` here would retire a staged event on any transient failure, i.e. destroy
            # the one durable copy this module exists to deliver.
            reason = exc.reason if isinstance(exc, SignatureError) else "invalid"
            log.warning("control_outbox_unverifiable", extra={"key": bounded(key), "action": _action_of(event_json), "reason": reason})
            await retire(key)
            continue
        try:
            await _republish(publisher, settings, envelope)
        except ValueError as exc:
            # THE BUS CAN NEVER CARRY IT: the publish seam's own refusal, made before any I/O, so no later tick
            # changes it. Left staged, it would stop every pass at itself and hold every event behind it.
            log.warning("control_outbox_undeliverable", extra={"key": bounded(key), "action": _action_of(event_json), "error": bounded(str(exc))})
            await retire(key)
            continue
        except Exception as exc:
            # The bus is still down. Stop the pass rather than grinding the whole backlog against it:
            # every remaining object stays staged and the next tick retries from the oldest.
            log.warning("control_outbox_republish_failed", extra={"key": key, "error": str(exc)})
            outbox_metrics.record_publish_failed()
            break
        if await drop_or_leave_staged(key):
            report.republished += 1
        else:
            outbox_metrics.record_drop_failed()
    outbox_metrics.record_drained(report.republished)
    return report


async def tick(ctx: Tick) -> ControlRelayReport:
    """One relay tick: verify and republish what is staged, oldest first."""
    report = await _drain(ctx.settings, get_control_publisher(ctx.request), await get_staged_verifier(ctx.request))
    if report.poison_dropped:
        log.warning("control_outbox_poison", extra={"dropped": report.poison_dropped})
    log.info(
        "control_relay_tick",
        extra={
            "depth": report.depth,
            "oldest_age_seconds": report.oldest_age_seconds,
            "republished": report.republished,
            "poison_dropped": report.poison_dropped,
            "left_staged": report.left_staged,
            "keys_unavailable": report.keys_unavailable,
        },
    )
    return report
