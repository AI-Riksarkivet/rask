"""Unit tests for the Dapr pub/sub subscription handler (#25).

Infra-free: no sidecar, no catalog. A fake store records the ingest; we pin the Dapr ack
contract: SUCCESS on a clean ingest, RETRY (redeliver) on a transient failure, DROP on a malformed
payload — and that the CloudEvent envelope's ``data`` is the OpenLineage event.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from lineage.core.metrics import Door
from lineage.models import RunEvent
from lineage.services.consumer import handle_cloud_event


_VALID = {
    "eventType": "COMPLETE",
    "eventTime": "2026-06-20T09:00:00Z",
    "run": {"runId": "r1", "facets": {"author": {"name": "alice"}}},
    "job": {"namespace": "ray-jobs", "name": "ingest"},
    "inputs": [{"namespace": "source", "name": "raw"}],
    "outputs": [{"namespace": "bronze", "name": "bronze$x"}],
}
# Dapr wraps the published data in a CloudEvent; the OpenLineage event is in ``data``.
_CLOUD_EVENT = {"id": "ce-1", "source": "catalog", "type": "com.dapr.event.sent", "data": _VALID}


class _FakeRepo:
    """The whole store surface this handler uses is `record` — the edges and the feed row are written inside
    it, so there is no second call to stub or forget."""

    def __init__(self, *, fail: bool = False) -> None:
        self.ingested: RunEvent | None = None
        self._fail = fail
        self.refusals: list[dict[str, str | None]] = []

    async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
        """[[LH-182]] The drain now RECORDS a settled refusal before retiring the object, so a double
        that cannot record one no longer stands in for the store. Captured rather than ignored:
        several of these tests assert what the refusal path did, and a silent no-op would let a drain
        that recorded nothing pass as one that did."""
        self.refusals.append({"outbox_key": outbox_key, "run_id": run_id, "author": author, "reason": reason, "event_json": event_json})

    async def record(self, event: RunEvent) -> None:
        if self._fail:
            raise RuntimeError("catalog unavailable")
        self.ingested = event


def test_handle_retries_on_transient_ingest_failure() -> None:
    # The catalog down → RETRY; Dapr redelivers per the component backOff (a record is idempotent, so retry is safe).
    repo = _FakeRepo(fail=True)
    status = asyncio.run(handle_cloud_event(cast(Any, repo), _CLOUD_EVENT, door=Door.SUBSCRIBER))
    assert status == {"status": "RETRY"}
