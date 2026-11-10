"""What maintenance's two processes stand on (ADR 0198).

The web process (:mod:`maintenance.service`) serves the write-event lane, and the worker process
(:mod:`maintenance.worker`) runs the queue's tasks. Both read the store under this service's session and both carry a
sidecar whose bus connections are watched (:func:`standing`); only the worker signs and emits, so only it holds the
signing key, the sidecar client and the two emitters (:func:`collaborators`). Each is built once at its process's
start and stopped at its end, and a start that fails part-way stops what it had started.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager, suppress

from dapr.aio.clients import DaprClient
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from lakehouse.bus_watchdog import start_bus_watchdog
from lakehouse.control_emit import ControlEmitter, ControlSign, make_control_emitter
from lakehouse.governed.signing_key import SigningKeyHolder, attach_signing, make_signing_holder
from lakehouse.lance_metrics import instrument_lance_if_available
from lakehouse.lineage import SigningKey, attach_control_signature, parse_published_keys
from lakehouse.store_session import require_env_pair, start_store_session
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import MaintenanceEmitter, make_emitter


class Collaborators(BaseModel):
    """What the worker runs its tasks with, built once at its start."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    #: This service's signing key, or ``None`` for a stack with no store or no identity.
    signing: SigningKeyHolder[SigningKey] | None
    lineage: MaintenanceEmitter
    control: ControlEmitter

    def ready(self) -> bool:
        """Whether what a pass emits can be signed: a worker waiting for its key takes no task."""
        return self.signing is None or self.signing.ready


def _control_sign(holder: SigningKeyHolder[SigningKey]) -> ControlSign:
    """The purge's control events, signed as this service with no delegation: maintenance authenticates nobody, so the
    actor it stamps is itself (`purge.ACTOR`) and it never signs for a person."""
    return lambda envelope: attach_control_signature(envelope, key=holder.key(), identity=holder.identity)


@asynccontextmanager
async def standing(app: FastAPI, settings: MaintenanceSettings) -> AsyncGenerator[None]:
    """The store session and the bus watchdog, for either process, stopped on the way out."""
    async with AsyncExitStack() as stack:
        instrument_lance_if_available()  # Lance-native IO metrics onto the global MeterProvider
        # MAINTENANCE HOLDS NO STORE KEY ([[XC-084]], ADR 0170). Its identity at the store is the ServiceAccount token its
        # pod projects for the store's audience, exchanged here for a session before the first read, failing the start
        # closed if none is issued, then exchanged again once half the session's life is used. Its own reads and records
        # sign with that session; every rewrite signs with its table's vend. With no token (the offline and Dagger lanes)
        # the environment's pair signs, and is refused here when either half is missing.
        store_session = await start_store_session(settings.web_identity, session_name="maintenance", duration_seconds=settings.s3_session_seconds)
        if store_session is None:
            require_env_pair(settings.env_pair, prefix="MAINTENANCE")
        else:
            stack.push_async_callback(store_session.stop)
        app.state.store_session = store_session
        # Restarts this pod's sidecar once one of its bus connections has closed for good, as daprd's do after a long bus
        # outage (`lakehouse.bus_watchdog`); None where the chart names this pod no bus.
        bus_watchdog = await start_bus_watchdog()
        if bus_watchdog is not None:
            stack.push_async_callback(bus_watchdog.stop)
        yield


async def _closed_quietly(client: DaprClient) -> None:
    """Close the sidecar client; a close the sidecar no longer answers is the end it was closed for."""
    with suppress(Exception):
        await client.close()


@asynccontextmanager
async def collaborators(app: FastAPI, settings: MaintenanceSettings) -> AsyncGenerator[Collaborators]:
    """Build what the worker emits with (its signing key, the sidecar client and the two emitters), publish the key on
    ``app.state`` for the readiness probe that reads it there, and stop it all on the way out."""
    async with AsyncExitStack() as stack:
        # ONE sidecar client for both emitters, lineage (data) and control (governance). Built when either is on; the
        # sidecar is local, so construction is cheap and needs no broker reachability at start.
        dapr_client = DaprClient() if (settings.lineage_emit_enabled or settings.control_emit_enabled) else None
        if dapr_client is not None:
            stack.push_async_callback(_closed_quietly, dapr_client)
        # THIS SERVICE'S OWN SIGNING KEY ([[LH-064]]), or None for a stack with no store or no identity. It is also the
        # author every event stamps, which the emitter checks at construction: a verifier refuses a signer that is not
        # its event's author.
        signing = make_signing_holder(
            secrets_from_dapr=settings.secrets_from_dapr,
            identity=settings.signing_identity,
            store=settings.dapr_secret_store,
            load_key=SigningKey.from_seed,
            parse_published=parse_published_keys,
        )
        attach_signing(app, signing)
        lineage = make_emitter(
            enabled=settings.lineage_emit_enabled,
            dapr=dapr_client,
            pubsub=settings.lineage_pubsub,
            topic=settings.lineage_topic,
            job_namespace=settings.lineage_job_namespace,
            dataset_namespace=settings.lineage_dataset_namespace,
            timeout_seconds=settings.publish_timeout_seconds,
            outbox_uri=settings.lineage_outbox_uri,
            storage_options=settings.storage_options,
            # The SAME identity this service presents at the catalog's service door, so the graph and the
            # catalog cannot disagree about who maintained a dataset. A signer must sign as it: the emitter refuses to
            # be built with a key for any other identity.
            author=settings.catalog_service_identity,
            signing=signing,
        )
        # #79: the expired-trash purge announces each reclamation on the catalog's control topic. A no-op
        # when off, never a half-configured transport that looks like it publishes.
        control = make_control_emitter(
            enabled=settings.control_emit_enabled,
            dapr=dapr_client,
            pubsub=settings.control_pubsub,
            timeout_seconds=settings.publish_timeout_seconds,
            service="maintenance",
            # SIGNED AS THIS SERVICE ([[XC-078]]) with the holder its lineage signs with; None for a stack with no store
            # or no identity. Nothing stages a control event here, so one the purge emits while the key is unresolved is
            # withheld and counted, never published unsigned.
            sign=_control_sign(signing) if signing is not None else None,
        )
        # RESOLVED LAST, so the probes answer from the moment the process starts whether or not the key resolved: a
        # worker waiting for its key takes no task, reports itself not ready, and heals in place when the key is
        # published.
        if signing is not None:
            await signing.start()
            stack.push_async_callback(signing.stop)
        yield Collaborators(signing=signing, lineage=lineage, control=control)
