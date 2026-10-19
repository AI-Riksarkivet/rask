"""[[CTL-33]] The notification for a catalog write names the run its writer named.

The catalog's write event carries the writer's run in its `lance` facet as `runId`, the key lineage's edge list reads
too, and notifications projects the producer's own run from the same facet. One key spelled two ways is a silent loss:
the notification names no run, so the inbox row and the email's `Run:` line lose the one id the writer's own tooling
answers to.

The catalog's own event builder on one side and notifications' own projection on the other, with nothing between.
"""

from __future__ import annotations

from datetime import UTC, datetime

from catalog.core.lineage_emit import build_write_event
from catalog.core.run_context import RunContext
from notifications.api.lineage_events import LineageRunEvent, notifiable


def test_a_catalog_writes_notification_carries_the_run_its_writer_named() -> None:
    event = build_write_event(
        table_id="lh470$bronze$scans",
        namespace="lh470",
        author="dex~alice",
        version=3,
        operation="insert",
        run_id="7c1f8a52-5a0e-4c39-9d1e-0f0b8f4c2a11",
        event_time=datetime(2026, 10, 19, 12, 0, tzinfo=UTC).isoformat(),
        job_namespace="rask",
        run=RunContext(run_id="pipeline-run-42"),
        project="lh470",
    )

    notice = notifiable(LineageRunEvent.model_validate(event))

    assert notice is not None, "a catalog COMPLETE authored by a person is notifiable"
    assert notice.delivery.source_run_id == "pipeline-run-42"
