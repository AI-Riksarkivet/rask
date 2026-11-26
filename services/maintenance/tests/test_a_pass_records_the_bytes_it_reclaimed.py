"""The number a reclaimer exists to produce must be a metric, not only a log line.

[[LH-099]]. With `bytes_removed` only on the per-dataset outcome and the audit line, and no instrument, the only bytes
series the maintenance service exports is `maintenance.trash.bytes_reclaimed`, fed solely by the PURGE, so "how much did
a pass get back" has no answer that outlives a log rotation. Measured on the deployed estate 2026-09-19, under the names
of that day: `compaction_bytes_reclaimed_total` returns ZERO series from GreptimeDB while `compaction_runs_total`
returns one, so a pass is observable except in the dimension it is for.

It is also [[LH-102]]'s closing condition, which asks for "a recorded reclaimed-bytes figure". A figure
recorded only in a log line is recorded for as long as the log lives.

THIS HALF NEEDS NO DECISION, and the row says so. The control-EVENT question beside it — whether a
person should be TOLD a table was compacted — is a separate owner call and is not touched here.

Driven through one dataset's real pass on pylance (`table_pass.maintain_dataset`), with the catalog's vend behind respx,
and read back from a real OpenTelemetry reader: the series carries the bytes the pass took off the store.
"""

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import httpx
import lance
import pyarrow as pa
import pytest
import respx
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from tests.vend_answers import vended

from maintenance.core import metrics
from maintenance.core.config import MaintenanceSettings
from maintenance.services.expiry import DatasetResult
from maintenance.services.plan import DatasetPlan


CATALOG = "http://catalog.test"

pytestmark = pytest.mark.usefixtures("catalog_calls_carry_no_identity")


def _bytes_under(uri: str) -> int:
    return sum(path.stat().st_size for path in Path(uri).rglob("*") if path.is_file())


def _reclaimed(reader: InMemoryMetricReader) -> list[int]:
    """Every point of `maintenance.bytes.reclaimed` as ``reader`` exports it."""
    data = reader.get_metrics_data()
    return [
        int(point.value)
        for resource in (data.resource_metrics if data is not None else ())
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "maintenance.bytes.reclaimed"
        for point in metric.data.data_points
        if isinstance(point, NumberDataPoint)
    ]


def test_a_reclaiming_pass_records_the_bytes_it_took_off_the_store(
    tmp_path: Path, maintain_one: Callable[..., DatasetResult], monkeypatch: pytest.MonkeyPatch
) -> None:
    uri = str(tmp_path / "t.lance")
    for batch in range(6):
        lance.write_dataset(pa.table({"id": pa.array(range(batch * 10, batch * 10 + 10), pa.int64())}), uri, mode="overwrite" if batch else "create")
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(metrics, "_bytes_reclaimed", provider.get_meter("lance.maintenance").create_counter("maintenance.bytes.reclaimed", unit="By"))
    settings = MaintenanceSettings.model_validate({"s3_bucket": "unused", "catalog_url": CATALOG, "min_versions_to_expire": 1})
    before = _bytes_under(uri)

    with respx.mock(assert_all_called=False) as door:
        options = {"aws_access_key_id": "VENDED", "aws_secret_access_key": "vended-secret"}
        door.post(f"{CATALOG}/management/v1/table/db$t/credentials", params={"tier": "maintain"}).mock(
            return_value=httpx.Response(200, json=vended(options, location=uri))
        )
        result = maintain_one(uri, settings, plan=DatasetPlan(older_than=timedelta(0), retain_versions=1))

    freed = before - _bytes_under(uri)
    recorded = _reclaimed(reader)
    provider.shutdown()
    assert (result.error, result.old_versions_removed, freed > 0) == (None, 5, True)
    assert recorded == [freed]
