"""Unit tests for the Dapr pub/sub subscription handler (#25).

Infra-free: no sidecar, no database. A fake repository records the ingest; we pin the Dapr ack
contract: SUCCESS on a clean ingest, RETRY (redeliver) on a transient failure, DROP on a malformed
payload — and that the CloudEvent envelope's ``data`` is the OpenLineage event.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from lineage.models import RunEvent
from lineage.services.consumer import handle_cloud_event
from lineage.services.repository import feed_columns


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
    """The whole repository surface this handler uses is `ingest_event` — the graph write and the
    durable /events row are one transaction inside it, so there is no second call to stub or forget."""

    def __init__(self, *, fail: bool = False) -> None:
        self.ingested: RunEvent | None = None
        self._fail = fail

    async def ingest_event(self, event: RunEvent) -> None:
        if self._fail:
            raise RuntimeError("AGE unavailable")
        self.ingested = event


def test_handle_ingests_and_acks_a_valid_cloud_event() -> None:
    repo = _FakeRepo()
    status = asyncio.run(handle_cloud_event(cast(Any, repo), _CLOUD_EVENT))
    assert repo.ingested is not None  # graph write + durable feed row, in one call
    assert status == {"status": "SUCCESS"}


def test_handle_drops_a_malformed_payload() -> None:
    # data that won't parse → redelivering is pointless, so DROP (don't poison the subscription).
    repo = _FakeRepo()
    status = asyncio.run(handle_cloud_event(cast(Any, repo), {"data": {"not": "an event"}}))
    assert repo.ingested is None
    assert status == {"status": "DROP"}


def test_handle_retries_on_transient_ingest_failure() -> None:
    # AGE down → RETRY; Dapr redelivers per the component backOff (ingest is idempotent, so retry is safe).
    repo = _FakeRepo(fail=True)
    status = asyncio.run(handle_cloud_event(cast(Any, repo), _CLOUD_EVENT))
    assert status == {"status": "RETRY"}


def test_the_feed_columns_project_the_event() -> None:
    event = RunEvent.model_validate(_VALID)
    fields = feed_columns(event)
    assert fields["run_id"] == "r1"
    assert fields["job"] == "ray-jobs/ingest"
    assert fields["author"] == "alice"
    assert fields["inputs"] == ["raw"] and fields["outputs"] == ["bronze$x"]
    assert fields["event"]["eventType"] == "COMPLETE"  # the full event JSON is preserved
