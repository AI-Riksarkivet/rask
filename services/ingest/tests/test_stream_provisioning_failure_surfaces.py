"""A stream-provisioning FAILURE must raise; only "already exists" is a non-event.

open_python-audit `ingest-flow-17` (E3, low, effort S): `ensure_stream` and `ensure_dlq_stream`
wrapped `add_stream` in a bare `except Exception` that logged DEBUG "already exists". Any real
failure — broker down mid-call, JetStream not enabled, an auth rejection, a malformed config — was
misreported as the normal in-cluster path, and the first symptom moved downstream to a publish
dying with `NoStreamResponseError` and nothing in the logs naming why the stream is missing.

The one benign signal is the server's own: err_code 10058, "stream name already in use with a
different configuration" (an add_stream with an IDENTICAL config succeeds outright, so 10058 is the
only shape "already exists" ever raises as).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from ingest.queue import WorkQueue
from nats.js import api as jsapi
from nats.js.errors import BadRequestError, ServiceUnavailableError


if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient
    from nats.js import JetStreamContext


class _JS:
    """`add_stream` raises what it is told; `stream_info` records that the exists-path went on to
    verify retention rather than returning blind."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.stream_info_reads = 0

    async def add_stream(self, config: jsapi.StreamConfig) -> None:
        raise self._exc

    async def stream_info(self, name: str) -> jsapi.StreamInfo:
        self.stream_info_reads += 1
        raise TimeoutError("not the subject under test")


def _queue(exc: Exception) -> tuple[WorkQueue, _JS]:
    js = _JS(exc)
    return WorkQueue(cast("NatsClient", object()), cast("JetStreamContext", js)), js


@pytest.mark.asyncio
async def test_a_broken_broker_is_not_read_as_already_exists() -> None:
    queue, _ = _queue(ServiceUnavailableError(code=503, description="JetStream system temporarily unavailable"))
    with pytest.raises(ServiceUnavailableError):
        await queue.ensure_stream()


@pytest.mark.asyncio
async def test_a_non_jetstream_failure_propagates_too() -> None:
    """A connection reset raises a plain client error, not an APIError — it must not be swallowed."""
    queue, _ = _queue(RuntimeError("nats: connection reset"))
    with pytest.raises(RuntimeError, match="connection reset"):
        await queue.ensure_stream()


@pytest.mark.asyncio
async def test_the_dlq_ensure_has_the_same_contract() -> None:
    queue, _ = _queue(ServiceUnavailableError(code=503, description="JetStream system temporarily unavailable"))
    with pytest.raises(ServiceUnavailableError):
        await queue.ensure_dlq_stream()


@pytest.mark.asyncio
async def test_already_exists_stays_a_non_event_and_still_checks_retention() -> None:
    """The narrow path keeps its old behaviour: 10058 is the chart's Job having got there first."""
    exists = BadRequestError(code=400, err_code=10058, description="stream name already in use with a different configuration")
    queue, js = _queue(exists)
    await queue.ensure_stream()  # must not raise
    assert js.stream_info_reads == 1, "the exists path must still go on to compare the live retention"


@pytest.mark.asyncio
async def test_already_exists_is_benign_for_the_dlq_too() -> None:
    exists = BadRequestError(code=400, err_code=10058, description="stream name already in use with a different configuration")
    queue, _ = _queue(exists)
    await queue.ensure_dlq_stream()  # must not raise
