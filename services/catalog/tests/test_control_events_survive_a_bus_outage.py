"""A dropped `table_published` cancels the cascade, and the emitter was documented as losing nothing.

`DaprControlEmitter.emit` swallowed every publish failure into a counter whose own description read
"fail-open: the change itself still happened and is audited, only the live-refresh hint is lost", and
the publication endpoint asserted "a consumer that MISSES this event loses nothing: the `published`
tag still answers 'what is ready?'".

Both claims hold for the catalog's console ring buffer and for any POLLING consumer. Both are false
for the cascade, which is the consumer that matters: the mover does not fire its own topic, so the
next hop happens ONLY when `/publication-arrival` receives this event -- and the medallion plane runs
no cron and no reconcile binding, so it never re-reads the tag. A 5s publish timeout during a NATS
blip therefore ends the cascade outright: the tag advanced, the data IS consumable, the route returned
200, every pod is green, and the only signal is a counter that says nothing was lost.

Owner ruling 2026-08-25: extend the EXISTING outbox rather than build a second one. The swallow stays
-- `emit` is called after the change is made and audited, so raising would turn a delivered mutation
into a 500 the caller retries -- but the event is no longer gone: it is staged before the publish and
dropped only on ack.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from service_kit.control_emit import DaprControlEmitter
from service_kit.control_events import CatalogControlEvent


def _event() -> CatalogControlEvent:
    return CatalogControlEvent(action="table_published", object_type="table", object_id="table:acme-silver$features", actor=None)


class _Blip:
    """A sidecar that accepts nothing — the NATS blip, as the emitter meets it."""

    async def publish_event(self, **_kw: Any) -> None:
        raise TimeoutError("publish timed out")


@pytest.mark.asyncio
async def test_a_failed_control_publish_leaves_the_event_STAGED(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE WEDGE. Unstaged, this event is simply gone and the cascade stops."""
    from service_kit.lakehouse import outbox

    uri = f"file://{tmp_path}/control-outbox"
    emitter = DaprControlEmitter(cast("Any", _Blip()), pubsub="p", topic="catalog.control.v1", timeout_seconds=1.0, service="catalog", outbox_uri=uri)

    await emitter.emit(_event())

    staged = list(outbox.list_events(uri, {}))
    assert staged, "the control event was lost; nothing can re-publish it and the cascade is cancelled"


@pytest.mark.asyncio
async def test_a_SUCCESSFUL_publish_drops_the_staged_copy(tmp_path: Any) -> None:
    """Drop-on-ack. A staged object that outlives its delivery is one the relay re-publishes forever."""
    from service_kit.lakehouse import outbox

    class _Ok:
        async def publish_event(self, **_kw: Any) -> None:
            return None

    uri = f"file://{tmp_path}/control-outbox"
    emitter = DaprControlEmitter(cast("Any", _Ok()), pubsub="p", topic="catalog.control.v1", timeout_seconds=1.0, service="catalog", outbox_uri=uri)

    await emitter.emit(_event())

    assert list(outbox.list_events(uri, {})) == [], "the delivered event stayed staged"


@pytest.mark.asyncio
async def test_the_emit_still_does_NOT_raise_into_the_caller(tmp_path: Any) -> None:
    """The half that must not change. `emit` runs after the mutation is made and audited, so raising
    would turn a delivered change into a 500 the caller retries -- announcing it twice."""
    uri = f"file://{tmp_path}/control-outbox"
    emitter = DaprControlEmitter(cast("Any", _Blip()), pubsub="p", topic="catalog.control.v1", timeout_seconds=1.0, service="catalog", outbox_uri=uri)

    await emitter.emit(_event())  # must not raise


@pytest.mark.asyncio
async def test_with_NO_outbox_configured_the_behaviour_is_unchanged() -> None:
    """Opt-in, exactly like the lineage outbox: an unconfigured deployment still publishes plainly."""
    emitter = DaprControlEmitter(cast("Any", _Blip()), pubsub="p", topic="catalog.control.v1", timeout_seconds=1.0, service="catalog")

    await emitter.emit(_event())  # must not raise, and must not need a store


def test_the_counter_no_longer_asserts_the_loss_is_FREE() -> None:
    """The finding's minimum bar, and worth its own test: prose that tells an operator a dropped event
    costs nothing is what made this invisible for as long as it was."""
    emitter = DaprControlEmitter(cast("Any", _Blip()), pubsub="p", topic="catalog.control.v1", timeout_seconds=1.0, service="catalog")
    description = emitter.failure_description

    assert "only the live-refresh hint is lost" not in description, "the counter still claims a dropped control event costs nothing"
    assert "cascade" in description.lower(), f"the counter does not warn about the consumer that cannot recover: {description!r}"
