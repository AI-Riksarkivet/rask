"""A dropped `table_published` cancels the cascade, and the emitter was documented as losing nothing.

`DaprControlEmitter.emit` swallowed every publish failure into a counter whose own description read
"fail-open: the change itself still happened and is audited, only the live-refresh hint is lost", and
the publication endpoint asserted "a consumer that MISSES this event loses nothing: the `published`
tag still answers 'what is ready?'".

Both claims hold for any POLLING consumer. Both are false
for a pipeline whose next hop happens ONLY when it receives this event and that runs no cron and no
reconcile, so it never re-reads the tag. A 5s publish timeout during a NATS blip therefore ends that
cascade outright: the tag advanced, the data IS consumable, the route returned
200, every pod is green, and the only signal is a counter that says nothing was lost.

Owner ruling 2026-08-25: extend the EXISTING outbox rather than build a second one. The swallow stays
-- `emit` is called after the change is made and audited, so raising would turn a delivered mutation
into a 500 the caller retries -- but the event is no longer gone: it is staged before the publish and
dropped only on ack.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from lakehouse.control_emit import DaprControlEmitter
from lakehouse.control_events import CatalogControlEvent
from lakehouse.storage_options import StorageOptions
from tests.unit.no_store_session import no_store_session


def _event() -> CatalogControlEvent:
    return CatalogControlEvent(action="table_published", object_type="table", object_id="table:acme-silver$features", actor=None)


class _Sidecar:
    """The sidecar's publish, with `dapr.aio.clients.DaprClient.publish_event`'s signature: it accepts, or it times out."""

    def __init__(self, *, answers: bool) -> None:
        self._answers = answers

    async def publish_event(
        self,
        pubsub_name: str,
        topic_name: str,
        data: bytes | str,
        publish_metadata: dict[str, str] | None = None,
        metadata: tuple[tuple[str, str | bytes], ...] | None = None,
        data_content_type: str | None = None,
    ) -> None:
        if not self._answers:
            raise TimeoutError("the sidecar did not answer")


@pytest.mark.asyncio
async def test_a_SUCCESSFUL_publish_drops_the_staged_copy(tmp_path: Any) -> None:
    """Drop-on-ack. A staged object that outlives its delivery is one the relay re-publishes forever."""
    from lakehouse import outbox

    uri = f"file://{tmp_path}/control-outbox"
    sidecar = cast("Any", _Sidecar(answers=True))
    emitter = DaprControlEmitter(sidecar, pubsub="p", topic="catalog.control.v1", timeout_seconds=1.0, service="catalog", sign=None, outbox_uri=uri)

    await emitter.emit(_event())

    assert list(outbox.list_events(uri, StorageOptions())) == [], "the delivered event stayed staged"


@pytest.mark.parametrize(
    ("outbox_at", "storage_options", "staged"),
    [
        pytest.param("control-outbox", None, True, id="the-outbox-holds-it"),
        pytest.param("occupied/control-outbox", None, False, id="the-stage-fails"),
        pytest.param("control-outbox", no_store_session, False, id="the-store-session-is-gone"),
    ],
)
@pytest.mark.asyncio
async def test_a_failed_publish_says_whether_the_outbox_holds_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, outbox_at: str, storage_options: Callable[[], StorageOptions] | None, staged: bool
) -> None:
    """A failed publish's own log line says whether the relay can still deliver the event, so an operator reading it
    knows whether anything was lost; a stage that failed, or never began, left the relay nothing ([[LH-573]])."""
    (tmp_path / "occupied").write_text("a file where the outbox's directory would be")
    sidecar = cast("Any", _Sidecar(answers=False))
    uri = f"file://{tmp_path}/{outbox_at}"
    emitter = DaprControlEmitter(
        sidecar, pubsub="p", topic="catalog.control.v1", timeout_seconds=1.0, service="catalog", sign=None, outbox_uri=uri, storage_options=storage_options
    )

    await emitter.emit(_event())

    assert [getattr(record, "staged", None) for record in caplog.records if record.getMessage() == "control_publish_failed"] == [staged]
