"""A sidecar whose bus connection closed for good is restarted once the bus answers again ([[XC-095]]).

THE DEFECT IS UPSTREAM, AND THIS MODULE IS ITS WORKAROUND UNTIL A DAPR RELEASE FIXES IT. Dapr 1.18.1's `pubsub.jetstream`
component (components-contrib v1.18.0, `pubsub/jetstream/jetstream.go:60-77`) connects with none of nats.go's reconnect
options, so nats.go v1.39.1's defaults hold: each server is tried 60 times, 2 s apart (`nats.go:53-61`). An outage that
outlasts them closes the connection for good (`nats.go:2985-2990`): every subscription is dropped, every publish answers
`nats: connection closed`, and daprd re-creates no component without a restart, since the estate runs without Dapr's
hot reload. Its probes stay green, the sidecar's liveness a TCP check and its readiness blind to components. Measured on
the e2e-stack lane 2026-10-17 (runs 39041664646, 39041873349, 39041873578): with NATS held down 150 s, lineage's and
notifications' durables acknowledged nothing for the sixteen minutes that followed and both of the catalog's outboxes
kept their backlog, every pod Ready. The defect is components-contrib#4563 and its fix #4564 (`MaxReconnects(-1)`); once
a Dapr release ships that fix, this module and its callers are deleted.

EACH SERVICE WATCHES ITS OWN SIDECAR. Every 30 s it asks the sidecar which `pubsub.jetstream` components it holds
(`/v1.0/metadata`) and publishes an empty probe through each to `probe.<app-id>`, a subject the app's bus user may
publish (`nats.auth.users`) and no stream captures. A live connection answers `nats: no response from stream` and stores
nothing; a reconnecting one times out waiting for its acknowledgement; a closed one answers `nats: connection closed`
(dapr `pkg/api/errors/pubsub.go:78` over nats.go's `ErrConnectionClosed`).

A CLOSED CONNECTION WAITS FOR THE BUS. The sidecar is asked to shut down (`POST /v1.0/shutdown`) only once a server the
bus names greets a new connection, and the kubelet starts it again with a new connection: its durable consumers resume
at their acknowledgement floors, and a subscription that fails while JetStream settles is retried by daprd itself (dapr
`pkg/runtime/processor/subscriber/subscriber.go:354-360`). Never sooner: a sidecar started while the bus refuses
connections fails its component and exits (dapr `pkg/runtime/processor/components.go:178-182`), which would hold the
pod out of its Service for as long as the bus is gone. A random delay of up to 30 s spreads a release's replicas, so its
Service keeps a ready pod while each restarts. The sidecar's API answers only on the pod's own loopback (the injector's
default `[::1],127.0.0.1`), so nothing outside the pod can ask it to shut down.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from contextlib import suppress
from typing import Final
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


log = logging.getLogger(__name__)

#: How often the sidecar's connections are probed.
CHECK_SECONDS: Final = 30.0
#: The longest a replica waits, once the bus answers, before asking its sidecar to restart.
SPREAD_SECONDS: Final = 30.0
#: Longer than nats.go waits for a publish's acknowledgement (5 s), so a reconnecting connection is told apart by the
#: sidecar's answer rather than by this client giving up first.
PROBE_TIMEOUT_SECONDS: Final = 10.0
#: How long a server the bus names has to send its greeting.
GREETING_TIMEOUT_SECONDS: Final = 2.0
#: A probe's subject is this and the app-id: published by that app's bus user alone, captured by no stream.
PROBE_PREFIX: Final = "probe."
#: What the sidecar answers for a publish through a connection nats.go has closed.
CLOSED: Final = "nats: connection closed"
JETSTREAM: Final = "pubsub.jetstream"
NATS_PORT: Final = 4222


class BusWatchdogSettings(BaseSettings):
    """The bus, as the chart names it to each pod whose sidecar connects to it, and the sidecar's own port."""

    model_config = SettingsConfigDict(extra="ignore")

    bus_url: str = Field(default="", alias="RASK_BUS_URL")
    #: DAPR'S OWN NAME, injected by daprd into every annotated pod.
    dapr_http_port: int = Field(default=3500, alias="DAPR_HTTP_PORT")


class _Component(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    type: str


class _Metadata(BaseModel):
    """What the watchdog reads of the sidecar's `/v1.0/metadata`: its app-id and the components it holds."""

    model_config = ConfigDict(extra="ignore")

    id: str
    components: list[_Component] = []


def bus_servers(url: str) -> list[tuple[str, int]]:
    """The servers a NATS URL names, as host and port: one URL or a comma-separated list, in any scheme."""
    servers: list[tuple[str, int]] = []
    for part in url.split(","):
        split = urlsplit(part.strip())
        if split.hostname:
            servers.append((split.hostname, split.port or NATS_PORT))
    return servers


async def bus_answers(servers: Sequence[tuple[str, int]]) -> bool:
    """Whether a server ``servers`` names greets a new connection as NATS does, with its `INFO` line before anything is
    sent. A proxy can accept a connection for a server that is gone, so a connection alone is not an answer."""
    for host, port in servers:
        try:
            async with asyncio.timeout(GREETING_TIMEOUT_SECONDS):
                reader, writer = await asyncio.open_connection(host, port)
                try:
                    greeting = await reader.readline()
                finally:
                    writer.close()
                    with suppress(OSError):
                        await writer.wait_closed()
        except (OSError, TimeoutError):
            continue
        if greeting.startswith(b"INFO "):
            return True
    return False


class BusWatchdog:
    """Probes one sidecar's bus connections, and restarts the sidecar once a closed one could connect again."""

    def __init__(self, *, sidecar: httpx.AsyncClient, servers: Sequence[tuple[str, int]], every: float = CHECK_SECONDS, spread: float = SPREAD_SECONDS) -> None:
        self._sidecar = sidecar
        self._servers = list(servers)
        self._every = every
        self._spread = spread
        self._waiting = False
        self._task: asyncio.Task[None] | None = None

    async def check(self) -> bool:
        """One look at the sidecar; whether it was asked to restart."""
        closed = await self._closed_components()
        if not closed:
            self._waiting = False
            return False
        if not await bus_answers(self._servers):
            # Logged once an episode: the wait is checked every pass and would repeat itself.
            if not self._waiting:
                log.warning("sidecar_bus_closed_waiting_for_the_bus", extra={"components": closed})
            self._waiting = True
            return False
        await asyncio.sleep(random.uniform(0.0, self._spread))  # noqa: S311 — spreads replicas, guards nothing
        log.warning("sidecar_bus_closed_restarting_the_sidecar", extra={"components": closed})
        (await self._sidecar.post("/v1.0/shutdown")).raise_for_status()
        self._waiting = False
        return True

    async def _closed_components(self) -> list[str]:
        try:
            answer = await self._sidecar.get("/v1.0/metadata")
            answer.raise_for_status()
            metadata = _Metadata.model_validate_json(answer.content)
        except (httpx.HTTPError, ValidationError):
            # A sidecar that does not answer is starting or stopping, which its own probes judge.
            return []
        return [component.name for component in metadata.components if component.type == JETSTREAM and await self._closed(component.name, metadata.id)]

    async def _closed(self, component: str, app_id: str) -> bool:
        try:
            answer = await self._sidecar.post(f"/v1.0/publish/{component}/{PROBE_PREFIX}{app_id}", json={}, timeout=PROBE_TIMEOUT_SECONDS)
        except httpx.HTTPError:
            return False
        return answer.status_code == 500 and CLOSED in answer.text

    async def start(self) -> None:
        """Probe in the background until `stop`."""
        self._task = asyncio.create_task(self._watch_forever(), name="bus-watchdog")

    async def stop(self) -> None:
        """End the background loop and release the sidecar client. Safe to call when it never started."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._sidecar.aclose()

    async def _watch_forever(self) -> None:
        while True:
            await asyncio.sleep(self._every)
            try:
                await self.check()
            except Exception:
                # A fault in a pass must not end the loop: the next pass is the only way a closed connection heals.
                log.exception("bus_watchdog_pass_failed")


async def start_bus_watchdog(settings: BusWatchdogSettings | None = None) -> BusWatchdog | None:
    """The watchdog, started, for a pod whose sidecar connects to the bus; None for one the chart names no bus to."""
    resolved = settings or BusWatchdogSettings()
    servers = bus_servers(resolved.bus_url)
    if not servers:
        return None
    watchdog = BusWatchdog(sidecar=httpx.AsyncClient(base_url=f"http://localhost:{resolved.dapr_http_port}"), servers=servers)
    await watchdog.start()
    return watchdog
