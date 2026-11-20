"""The handling of one delivery off the bus (#25), with a store that cannot record.

Infra-free: no bus, no catalog. A fake store fails the record, and the handling answers the lane to deliver the event
again (a record is idempotent, so a redelivery is safe), reading the OpenLineage event out of the CloudEvent envelope's
``data``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from typing import Any, cast

from lakehouse.bus_delivery import Verdict
from lakehouse.cloud_events import lineage_envelope
from lineage.core.metrics import Door
from lineage.models import RunEvent
from lineage.services.consumer import handle_cloud_event
from tests.open_gate import admits_everything


_VALID = {
    "eventType": "COMPLETE",
    "eventTime": "2026-06-20T09:00:00Z",
    "run": {"runId": "r1", "facets": {"rask_author": {"name": "alice"}}},
    "job": {"namespace": "pipelines", "name": "ingest"},
    "inputs": [{"namespace": "source", "name": "raw"}],
    "outputs": [{"namespace": "bronze", "name": "bronze$x"}],
}
#: The delivery a subscriber takes: the envelope its publisher built, with the OpenLineage event in ``data``.
_CLOUD_EVENT = lineage_envelope(_VALID).as_wire()


class _FakeRepo:
    """The whole store surface this handler uses is `record` — the edges and the feed row are written inside
    it, so there is no second call to stub or forget."""

    def __init__(self, *, fail: bool = False) -> None:
        self.ingested: RunEvent | None = None
        self._fail = fail
        self.refusals: list[dict[str, str | None]] = []

    async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
        """[[LH-182]] The drain RECORDS a settled refusal before retiring the object, so a double that cannot record one
        does not stand in for the store. Captured rather than ignored: a silent no-op would let a drain that recorded
        nothing pass as one that did."""
        self.refusals.append({"outbox_key": outbox_key, "run_id": run_id, "author": author, "reason": reason, "event_json": event_json})

    async def record(self, event: RunEvent, *, aliased: Collection[str]) -> None:
        if self._fail:
            raise RuntimeError("catalog unavailable")
        self.ingested = event


def test_handle_retries_on_transient_ingest_failure() -> None:
    # The catalog down → again; the lane delivers it again after its delay (a record is idempotent, so a retry is safe).
    repo = _FakeRepo(fail=True)
    verdict = asyncio.run(handle_cloud_event(cast(Any, repo), _CLOUD_EVENT, admits_everything, door=Door.SUBSCRIBER))
    assert verdict is Verdict.AGAIN
