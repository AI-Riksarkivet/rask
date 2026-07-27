"""The emitter core — where authored :class:`~lineage_kit.schemas.RunEvent`\\ s go.

One hard guarantee: **emission never crashes compute**. A missing endpoint degrades to a
logged no-op emitter; a transport failure on a configured emitter is caught and logged.
Lineage is telemetry — the pipeline's correctness must not depend on it.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Protocol

from openlineage.client import OpenLineageClient
from openlineage.client.transport.console import ConsoleConfig, ConsoleTransport
from openlineage.client.transport.http import HttpConfig, HttpTransport, create_token_provider

from lineage_kit.config import LineageSettings


if TYPE_CHECKING:
    from collections.abc import Iterator

    from lineage_kit.schemas import RunEvent

log = logging.getLogger(__name__)


class Emitter(Protocol):
    """Anything that can take an authored RunEvent. Implementations must never raise."""

    def emit(self, event: RunEvent) -> None: ...


class NoopEmitter:
    """No endpoint configured (or lineage forced off): log at debug, drop the event."""

    def emit(self, event: RunEvent) -> None:
        log.debug("lineage_noop_drop", extra={"job": event.job.name, "state": event.event_type.value})


class RecordingEmitter:
    """Collects events in memory — for tests (this package's and every adopter's)."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def emit(self, event: RunEvent) -> None:
        self.events.append(event)


class ClientEmitter:
    """Emits through an ``openlineage-python`` client; transport errors are logged, never raised."""

    def __init__(self, client: OpenLineageClient) -> None:
        self._client = client

    def emit(self, event: RunEvent) -> None:
        try:
            self._client.emit(event.to_openlineage())
        except Exception:
            log.warning("lineage_emit_failed", exc_info=True, extra={"job": event.job.name, "state": event.event_type.value})


def build_emitter(settings: LineageSettings | None = None) -> Emitter:
    """Construct the emitter the environment asks for (see :class:`LineageSettings`)."""
    s = settings or LineageSettings()
    kind = s.transport
    if kind == "auto":
        kind = "http" if s.endpoint else "noop"
    if kind == "console":
        return ClientEmitter(OpenLineageClient(transport=ConsoleTransport(ConsoleConfig())))
    if kind == "http":
        if not s.endpoint:
            log.warning("lineage_http_without_endpoint — set RASK_LINEAGE_ENDPOINT; degrading to no-op")
            return NoopEmitter()
        config = HttpConfig(
            url=s.endpoint,
            endpoint=s.endpoint_path,
            timeout=s.timeout,
            auth=create_token_provider({"type": "api_key", "apiKey": s.api_key}) if s.api_key else create_token_provider({}),
        )
        return ClientEmitter(OpenLineageClient(transport=HttpTransport(config)))
    log.debug("lineage_disabled (transport=%s, endpoint=%s)", s.transport, s.endpoint)
    return NoopEmitter()


_default: Emitter | None = None


def default_emitter() -> Emitter:
    """The process-wide emitter, built lazily from the environment on first use."""
    global _default  # deliberate process-wide singleton
    if _default is None:
        _default = build_emitter()
    return _default


def set_default_emitter(emitter: Emitter | None) -> None:
    """Inject the process-wide emitter (tests, app wiring). ``None`` resets to lazy env build."""
    global _default
    _default = emitter


#: The ambient emitter — how an emitter injected at the JOB level flows to the stage and
#: actor runs opened inside it (in-process only; a process boundary re-resolves from env).
_ambient: ContextVar[Emitter | None] = ContextVar("lineage_kit_ambient_emitter", default=None)


def ambient_emitter() -> Emitter | None:
    """The emitter of the enclosing run, if one is open in this context."""
    return _ambient.get()


@contextmanager
def use_emitter(emitter: Emitter) -> Iterator[Emitter]:
    """Make ``emitter`` ambient for the enclosed block (child runs inherit it)."""
    token = _ambient.set(emitter)
    try:
        yield emitter
    finally:
        _ambient.reset(token)
