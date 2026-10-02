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
from openlineage.client.transport.http import HttpConfig, HttpTransport, TokenProvider, create_token_provider

from lineage_kit.config import LineageSettings
from lineage_kit.identity import ProjectedTokenProvider
from lineage_kit.metrics import DropReason, record_drop


if TYPE_CHECKING:
    from collections.abc import Iterator

    from lineage_kit.schemas import RunEvent

log = logging.getLogger(__name__)


class Emitter(Protocol):
    """Anything that can take an authored RunEvent. Implementations must never raise.

    ``emit`` ANSWERS WHETHER THE EVENT NEEDS RECOVERY, and the distinction from "did it reach the
    graph" is the whole contract. False means this event is lost unless someone stages it; True means
    nothing is owed. Those differ for :class:`NoopEmitter`, which delivers nowhere and loses nothing.

    IT DOES NOT MAKE EMISSION FAIL THE RUN. Implementations still never raise — a run whose data landed
    must not be reported as failed because the graph was unreachable, which would turn an observability
    outage into a data incident. What the bool adds is that the caller can now ASK: the emitter already
    computes this fact to log and count it, and returning it is what lets a producer stage a refused
    event instead of discovering the gap months later in the graph.
    """

    def emit(self, event: RunEvent) -> bool: ...


class NoopEmitter:
    """No endpoint configured (or lineage forced off): log at debug, drop the event.

    ANSWERS TRUE, because the bool means "needs no recovery". A deployment that switched lineage off
    has lost nothing, and staging its events would grow an outbox that nothing drains — turning an
    opt-out into a leak.
    """

    def emit(self, event: RunEvent) -> bool:
        log.debug("lineage_noop_drop", extra={"job": event.job.name, "state": event.event_type.value})
        return True


class RecordingEmitter:
    """Collects events in memory — for tests (this package's and every adopter's)."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def emit(self, event: RunEvent) -> bool:
        self.events.append(event)
        return True


class ClientEmitter:
    """Emits through an ``openlineage-python`` client; failures are counted and logged, never raised.

    TWO failures, not one. Authoring (turning our model into the client's) and transport (getting it
    to the endpoint) sat inside a single ``try``, so a producer that built an unserialisable facet was
    reported with the same message as a lineage service that was down — and the log line was the only
    evidence either had happened. Both are still swallowed, because emission must never crash compute;
    both now leave a countable series (:mod:`lineage_kit.metrics`) and their own log event.
    """

    def __init__(self, client: OpenLineageClient) -> None:
        self._client = client

    def emit(self, event: RunEvent) -> bool:
        state = event.event_type.value
        try:
            payload = event.to_openlineage()
        except Exception:
            log.warning("lineage_author_failed", exc_info=True, extra={"job": event.job.name, "state": state})
            record_drop(DropReason.AUTHOR, state)
            return False
        try:
            self._client.emit(payload)
        except Exception:
            log.warning("lineage_emit_failed", exc_info=True, extra={"job": event.job.name, "state": state})
            record_drop(DropReason.TRANSPORT, state)
            return False
        return True


def _credential(s: LineageSettings) -> TokenProvider:
    """The one credential the transport presents: a configured static bearer, else the pod's identity token.

    One, never a combination: the door authenticates the ``Authorization`` bearer and nothing else
    names the caller, so a second credential beside it would carry no meaning.
    """
    if s.api_key:
        return create_token_provider({"type": "api_key", "apiKey": s.api_key})
    if s.identity_token_file is not None:
        return ProjectedTokenProvider(s.identity_token_file)
    return create_token_provider({})


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
        config = HttpConfig(url=s.endpoint, endpoint=s.endpoint_path, timeout=s.timeout, auth=_credential(s))
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
