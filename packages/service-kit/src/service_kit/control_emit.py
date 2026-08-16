"""Best-effort control-plane change-event emission onto the Dapr/NATS bus.

The publish half of :mod:`service_kit.control_events`, which owns the wire model. Every producer of a
governance mutation notice uses THIS module — the catalog (grants, warehouses, policies, namespaces,
tables), maintenance (the expired-trash purge destroys bytes and revokes grants), and the annotator
(a task is assigned to a named person).

**One implementation, and the history is the argument for it.** This began as a catalog module and was
copy-pasted into maintenance, whose docstring recorded the reason: *maintenance may not import the
catalog* (``tests/unit/test_declared_dependencies.py`` — declaring it would drag the catalog's whole
closure into that image). That justified not importing the CATALOG; it never justified a second
implementation. ``service_kit`` is importable by every service by construction, so the third producer
made the shared home obvious. ``tests/unit/test_invariants.py`` now fails on a fourth copy.

**Publish is inline-awaited and best-effort.** A mutation endpoint awaits it AFTER the backend/FGA
change and its audit succeed — so a change that did not happen is never announced — but every error is
swallowed and counted, so the bus being down can never fail the mutation. The fail-open posture matters
most where the caller cannot undo its work: maintenance's purge has already deleted bytes and revoked
tuples by the time it emits, and raising there would fail a reclamation that irreversibly happened.

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

import logging
from typing import Any, Protocol, runtime_checkable

from dapr.aio.clients import DaprClient
from opentelemetry import metrics

from service_kit import dapr_publish
from service_kit.control_events import (
    CONTROL_TOPIC,
    CatalogControlEvent,
    ControlAction,
    ControlObjectType,
)


log = logging.getLogger(__name__)


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
    """Publish the event to the Dapr ``pubsub.jetstream`` component via the local sidecar.

    Bounded by a tight per-publish timeout (a hung sidecar must not pin the inline-awaited emit on a
    mutation request path) and fully best-effort — every error swallowed, counted and logged.
    ``authorization`` is irrelevant: the topic is an internal channel, so the subscriber trusts the
    verified ``actor`` the producer stamped.

    ``service`` names the emitting service for telemetry ONLY. It is the single thing that differed
    between the two copies this module replaced, so it is a parameter rather than a reason to fork:
    the counter stays per-service (``<service>.control_emit.failed``) and an operator can still tell
    which producer's bus path is degrading.
    """

    def __init__(self, client: DaprClient, *, pubsub: str, topic: str, timeout_seconds: float, service: str) -> None:
        self._client = client
        self._pubsub = pubsub
        self._topic = topic
        self._timeout_seconds = timeout_seconds
        self._service = service
        self._emit_failed = metrics.get_meter(f"lance.{service}").create_counter(
            f"{service}.control_emit.failed",
            unit="{event}",
            description=(
                f"Best-effort {service} control-plane emits that failed terminally (fail-open: the change "
                "itself still happened and is audited, only the live-refresh hint is lost)."
            ),
        )

    async def emit(self, event: CatalogControlEvent) -> None:
        try:
            await dapr_publish.publish_event(
                self._client,
                timeout_seconds=self._timeout_seconds,
                pubsub_name=self._pubsub,
                topic_name=self._topic,
                data=event.model_dump_json(),
                data_content_type="application/json",
            )
        except Exception as exc:
            self._emit_failed.add(1, {f"lance.{self._service}.action": event.action})
            log.warning(
                "control_publish_failed",
                extra={"action": event.action, "object_id": event.object_id, "error": str(exc)},
            )


def make_control_emitter(
    *,
    enabled: bool,
    dapr: DaprClient | None,
    pubsub: str,
    service: str,
    topic: str = CONTROL_TOPIC,
    timeout_seconds: float,
) -> ControlEmitter:
    """The chosen control emitter: a Dapr publisher when enabled and a sidecar client is present, else
    the no-op (dev/off, like lineage). Built once in the service's lifespan onto
    ``app.state.control_emitter``."""
    if enabled and dapr is not None:
        return DaprControlEmitter(dapr, pubsub=pubsub, topic=topic, timeout_seconds=timeout_seconds, service=service)
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
