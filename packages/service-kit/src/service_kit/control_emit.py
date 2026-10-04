"""Best-effort control-plane change-event emission onto the Dapr/NATS bus, signed by the service that emits.

The publish half of :mod:`service_kit.control_events`, which owns the wire model. Every producer of a
governance mutation notice uses THIS module — the catalog (grants, warehouses, policies, namespaces,
tables), maintenance (the expired-trash purge destroys bytes and revokes grants), and the annotator
(a task is assigned to a named person).

**One implementation, and the history is the argument for it.** This began as a catalog module and was
copy-pasted into maintenance, whose docstring recorded the reason: *maintenance may not import the
catalog* (declaring it would drag the catalog's whole closure into that image). That justified not
importing the CATALOG; it never justified a second implementation. ``service_kit`` is importable by
every service by construction, so the third producer made the shared home obvious.

**Publish is inline-awaited and best-effort.** A mutation endpoint awaits it AFTER the backend/FGA
change and its audit succeed — so a change that did not happen is never announced — but every error is
swallowed and counted, so the bus being down can never fail the mutation. The fail-open posture matters
most where the caller cannot undo its work: maintenance's purge has already deleted bytes and revoked
tuples by the time it emits, and raising there would fail a reclamation that irreversibly happened.

**Signed at emit** ([[XC-078]]). A bus door authenticates the sidecar that delivered an event, not the service
that wrote it, so the actor an event names is a claim until its signature proves which service stamped it. The
emitter signs each event with its service's own key through the injected ``sign`` (this package cannot import
lineage-kit, which owns the wire format) BEFORE the event is staged, so an outbox holds the bytes the bus will
carry and a relay republishes them as they are, verifying and never signing (the catalog's ``control_relay``).
Nothing unsigned leaves a signer, and a signer stages nothing unsigned: an event it cannot sign, its key unresolved
or the event without a canonical form, is WITHHELD (neither staged nor published) and counted, as the catalog
withholds a lineage event it cannot sign, while the signer reports itself not ready. Staged bytes are authenticated
by nothing but the signature they carry, so nothing may sign them later: a relay that signed what it found staged
would sign whatever any other writer of the prefix put there. A service with no signing identity passes
``sign=None`` and publishes unsigned, which a door accepts only for an action
:func:`service_kit.control_events.control_signer_role` exempts.

**No broker client in app code.** Publishing goes through the local Dapr sidecar
(:func:`service_kit.dapr_publish.publish_event`), which owns retry, backoff and DLQ as component
config. Subscribers take the topic WITHOUT a ``queueGroupName``, so every replica receives every event
(broadcast — each replica's cache/ring-buffer stays complete).

Two ways this fails SILENTLY in-cluster, named because "best-effort" hides them:

* the pubsub component's ``scopes`` must list the producer's Dapr app-id, or the sidecar rejects every
  publish (the same class as the secret-store scoping rule);
* with control emission disabled, or no sidecar client, this is a no-op that publishes nowhere — never
  a half-configured transport pretending to emit.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Literal, Protocol, runtime_checkable

from dapr.aio.clients import DaprClient
from opentelemetry import metrics

from service_kit.control_events import (
    CONTROL_TOPIC,
    CatalogControlEvent,
    ControlAction,
    ControlObjectType,
)
from service_kit.lakehouse import outbox


log = logging.getLogger(__name__)

#: Signs one control event as the emitting service: given the JSON the bus will carry, read back, it returns a copy
#: carrying `rask_signature` (`lineage_kit.signing.attach_control_signature` over the service's own key), and raises
#: `SigningKeyUnavailableError` while that key is unresolved. A callable because this package cannot import lineage-kit.
type ControlSign = Callable[[dict[str, Any]], dict[str, Any]]

#: Why an emit did not reach the bus: the publish failed, or the event could not be signed.
type _Undelivered = Literal["publish", "unsigned"]


@runtime_checkable
class ControlEmitter(Protocol):
    """Emit one control-plane change-event. Total (never raises) — a bus failure degrades to no live
    refresh, never a failed mutation."""

    async def emit(self, event: CatalogControlEvent) -> None: ...


class NoopControlEmitter:
    """The off state (control emission disabled, or no Dapr transport). Every emit is a no-op."""

    async def emit(self, event: CatalogControlEvent) -> None:
        del event  # off state: no-op (`del`, not a per-line ARG002 suppression, so BOTH ruff and ty accept the unused arg;
        # the param must stay named `event` to structurally match the ControlEmitter protocol)


class DaprControlEmitter:
    """Publish the event to the Dapr ``pubsub.jetstream`` component via the local sidecar, signed by ``sign``.

    Bounded by a tight per-publish timeout (a hung sidecar must not pin the inline-awaited emit on a
    mutation request path) and fully best-effort — every error swallowed, counted and logged. The
    publish carries no ``authorization``: a subscriber learns which service stamped an event from its
    signature, because the transport authenticates only the sidecar that delivered it.

    ``service`` names the emitting service for telemetry ONLY. It is the single thing that differed
    between the two copies this module replaced, so it is a parameter rather than a reason to fork:
    the counter stays per-service (``<service>.control_emit.failed``) and an operator can still tell
    which producer's bus path is degrading.
    """

    def __init__(
        self,
        client: DaprClient,
        *,
        pubsub: str,
        topic: str,
        timeout_seconds: float,
        service: str,
        sign: ControlSign | None,
        outbox_uri: str = "",
        storage_options: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._pubsub = pubsub
        self._topic = topic
        self._timeout_seconds = timeout_seconds
        self._service = service
        #: This service's signer, or None for a service with no signing identity, which publishes unsigned.
        self._sign = sign
        #: The control lane's OWN outbox prefix — never the lineage one. Each prefix is drained by a
        #: lane-specific relay that re-ingests what it finds, so sharing would feed each the other's
        #: events. Empty means unstaged: a failed publish is lost.
        self._outbox_uri = outbox_uri
        self._storage_options = dict(storage_options or {})
        # What this description SAYS is load-bearing: an operator reads it to decide whether a rising
        # count lost anything, and the answer depends on the consumer and on whether an outbox stages.
        self._emit_failed = metrics.get_meter(f"lance.{service}").create_counter(
            f"{service}.control_emit.failed",
            unit="{event}",
            description=(
                f"{service} control-plane emits that did not reach the bus, by `reason`: `publish` (the bus "
                "refused or timed out) or `unsigned` (this service's signing key is unresolved, or it cannot sign "
                "the event at all). The change ITSELF still happened and is audited. Whether anything is "
                "lost depends on the consumer: a console ring buffer or a tag-polling reader loses only a refresh "
                "hint, but under medallion.cascadeViaPublish the downstream cascade rides `table_published` and "
                "does NOT poll — a dropped event there cancels the next hop outright. When an outbox is "
                "configured a failed publish stays STAGED and its relay delivers it. An `unsigned` event is never "
                "staged, whatever the configuration, and the service reports itself not ready meanwhile; so a rising "
                "`unsigned` count, or a rising `publish` count with no outbox, means cascades were silently abandoned."
            ),
        )

    async def emit(self, event: CatalogControlEvent) -> None:
        """Publish one control event, signed when this service signs, STAGED when an outbox is configured.

        Swallows every failure, and that part is deliberate: this is called after the change has already
        happened and been audited, so raising here would turn a delivered mutation into a 500 the caller
        retries. The event is staged before the publish and dropped only on ack, so a relay can re-publish
        it, and `event_id` is the client-side dedupe key, so a re-published event is recognised rather than
        double-applied.

        SIGNED BEFORE IT IS STAGED, so the staged copy is the signed bytes and a relay republishes them as
        they are. An event this service cannot sign, its key unresolved or the event without a canonical
        form, is WITHHELD: neither staged nor published, counted under `reason="unsigned"` and logged with
        its action and `event_id`. Published, a door would refuse it and acknowledge the refusal; staged,
        it could leave the outbox only if something signed it there, and nothing may sign what it finds
        staged, because staged bytes prove nothing about who wrote them.
        """
        event_json = event.model_dump_json()
        if self._sign is not None:
            try:
                event_json = json.dumps(self._sign(json.loads(event_json)))
            except Exception as exc:
                self._record_undelivered(event, exc, reason="unsigned", staged=False)
                return
        try:
            await outbox.publish_with_outbox(
                self._client,
                outbox_uri=self._outbox_uri,
                storage_options=self._storage_options,
                key=event.event_id,
                event_json=event_json,
                pubsub_name=self._pubsub,
                topic_name=self._topic,
                timeout_seconds=self._timeout_seconds,
            )
        except Exception as exc:
            self._record_undelivered(event, exc, reason="publish", staged=bool(self._outbox_uri))

    def _record_undelivered(self, event: CatalogControlEvent, error: Exception, *, reason: _Undelivered, staged: bool) -> None:
        self._emit_failed.add(1, {f"lance.{self._service}.action": event.action, f"lance.{self._service}.reason": reason})
        log.warning(
            "control_publish_failed" if reason == "publish" else "control_emit_withheld_unsigned",
            extra={
                "action": event.action,
                "event_id": event.event_id,
                "object_id": event.object_id,
                "error": str(error),
                # Whether this is recoverable at all, said in the line itself — an operator
                # reading it should not have to go and check how the service was configured.
                "staged": staged,
            },
        )


def make_control_emitter(
    *,
    enabled: bool,
    dapr: DaprClient | None,
    pubsub: str,
    service: str,
    sign: ControlSign | None,
    topic: str = CONTROL_TOPIC,
    timeout_seconds: float,
    outbox_uri: str = "",
    storage_options: dict[str, str] | None = None,
) -> ControlEmitter:
    """The chosen control emitter: a Dapr publisher when enabled and a sidecar client is present, else
    the no-op (dev/off, like lineage). Built once in the service's lifespan onto
    ``app.state.control_emitter``.

    ``sign`` has no default, so every producer states whether it signs: None publishes unsigned, which a
    door accepts only for an action ``control_signer_role`` exempts."""
    if enabled and dapr is not None:
        return DaprControlEmitter(
            dapr,
            pubsub=pubsub,
            topic=topic,
            timeout_seconds=timeout_seconds,
            service=service,
            sign=sign,
            outbox_uri=outbox_uri,
            storage_options=storage_options,
        )
    return NoopControlEmitter()


async def emit_control(
    emitter: ControlEmitter,
    *,
    action: ControlAction,
    object_type: ControlObjectType,
    object_id: str,
    actor: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Build and emit a ``CatalogControlEvent`` (best-effort — the emitter swallows every error).

    Call at a mutation endpoint AFTER the backend/FGA change and its audit succeed, so a change that did
    not happen is never announced. Endpoints obtain ``emitter`` via their ``ControlEmitterDep``; the off
    state is a ``NoopControlEmitter``, so the call is always safe and needs no ``getattr`` guard.
    ``actor`` must be the verified OIDC subject (e.g. ``user:alice``), never a request-body value.

    ``extra["subject"]`` is load-bearing for the notifications plane: for an action in that plane's
    ``NAMED_ACTIONS`` it names the PERSON the event is about, and being named IS the targeting. It must
    be a real principal — a userset or the ``user:*`` wildcard addresses nobody.

    Fail-open covers the WHOLE path, not just the publish: the event construction (pydantic validation)
    is wrapped too, so a malformed event can never raise into — and 500 — a mutation that already
    committed. A build failure degrades to no live-refresh hint, exactly like a publish failure.
    """
    try:
        event = CatalogControlEvent(
            action=action,
            object_type=object_type,
            object_id=object_id,
            actor=actor,
            extra=extra or {},
        )
    except Exception as exc:
        log.warning(
            "control_event_build_failed",
            extra={"action": action, "object_id": object_id, "error": str(exc)},
        )
        return
    await emitter.emit(event)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# The PROCESS-level emitter — for code with no request to resolve a dependency from
# ─────────────────────────────────────────────────────────────────────────────────────────────────
#
# Every HTTP producer reaches its emitter through a FastAPI dependency over `app.state`, which is the
# right shape: one emitter per process, resolved per request, overridable in a test.
#
# A DAPR ACTOR has no request. The annotator's lease reminder fires from `receive_reminder` on a timer
# — no principal, no `Request`, no dependency graph — and it is the edge whose audience most needs
# telling, because the person's hold on the task is being taken away while they are not looking. Its
# only options were a module-global or nothing.
#
# So: a process-scoped holder, set by the same lifespan that builds `app.state.control_emitter`, and
# defaulting to the NO-OP. The default matters — a service that never sets it (or one whose emit is
# disabled) gets silence rather than an AttributeError inside an actor turn, which is the same
# fail-open posture `emit_control` already has.
#
# NOT a replacement for the dependency. A request-path producer that reached for this would lose the
# per-test override that makes the emit assertable, and would couple itself to import order.
_PROCESS_EMITTER: ControlEmitter = NoopControlEmitter()


def set_process_control_emitter(emitter: ControlEmitter) -> None:
    """Publish the process's emitter for non-request callers. Called once, from the lifespan."""
    global _PROCESS_EMITTER  # one process-wide handle is the point
    _PROCESS_EMITTER = emitter


def process_control_emitter() -> ControlEmitter:
    """The process's emitter, or the no-op when the lifespan never set one."""
    return _PROCESS_EMITTER
