"""The hourly audit asks the store for each governed bucket's event configuration through a client of its own, and
closes it when the run ends ([[XC-219]]): a run every hour that left its client open would leave a connection pool
behind each time. What the store answers a real session is the `rustfs_provisioning` lane's claim
(`tests/e2e-py/test_the_store_answers_maintenance_which_buckets_send_store_events.py`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import EndpointConnectionError

from maintenance.core.config import MaintenanceSettings
from maintenance.services import bucket_events
from maintenance.services.bucket_events import report_bucket_events


class _Store:
    """The S3 client's two calls the audit makes, on a store that does not answer the first."""

    def __init__(self) -> None:
        self.closed = False

    def get_bucket_notification_configuration(self, *, Bucket: str) -> dict[str, Any]:  # noqa: N803 — the S3 client's own spelling
        raise EndpointConnectionError(endpoint_url=f"http://store/{Bucket}")

    def close(self) -> None:
        self.closed = True


def test_the_client_is_closed_when_the_run_ends_whatever_the_store_answered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    monkeypatch.setattr(bucket_events, "object_client", lambda _options: store)
    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"}).model_copy(update={"control_root": str(tmp_path / "control")})

    report = report_bucket_events(settings)

    assert report is not None
    assert (report.unknown, store.closed) == (["b"], True)
