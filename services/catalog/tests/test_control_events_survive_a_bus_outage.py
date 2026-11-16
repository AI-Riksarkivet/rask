"""A control event the bus did not take is staged, and the emitter says whether the outbox holds it.

`BusControlEmitter.emit` swallows a publish failure: it runs after the change is made and audited, so raising would
turn a delivered mutation into a 500 the caller retries. A polling consumer loses only a refresh hint. A pipeline whose
next step starts when it receives the event (a tag's move, say) and that polls nothing loses that step, so the event is
staged before the publish and dropped only on ack, and the relay delivers what stayed staged.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from lakehouse.control_emit import BusControlEmitter
from lakehouse.control_events import CatalogControlEvent
from lakehouse.storage_options import StorageOptions
from tests.bus_double import BusDouble
from tests.unit.no_store_session import no_store_session


def _event() -> CatalogControlEvent:
    return CatalogControlEvent(action="table_tag_updated", object_type="table", object_id="table:acme-silver$features", actor=None)


@pytest.mark.asyncio
async def test_a_SUCCESSFUL_publish_drops_the_staged_copy(tmp_path: Any) -> None:
    """Drop-on-ack. A staged object that outlives its delivery is one the relay re-publishes forever."""
    from lakehouse import outbox

    uri = f"file://{tmp_path}/control-outbox"
    emitter = BusControlEmitter(BusDouble(), topic="catalog.control.v1", timeout_seconds=1.0, service="catalog", sign=None, outbox_uri=uri)

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
    bus = BusDouble(failing=TimeoutError("the bus did not answer"))
    uri = f"file://{tmp_path}/{outbox_at}"
    emitter = BusControlEmitter(
        bus, topic="catalog.control.v1", timeout_seconds=1.0, service="catalog", sign=None, outbox_uri=uri, storage_options=storage_options
    )

    await emitter.emit(_event())

    assert [getattr(record, "staged", None) for record in caplog.records if record.getMessage() == "control_publish_failed"] == [staged]
