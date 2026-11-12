"""An `Idempotency-Key`'s record goes once it outlives its lifetime, at maintenance's daily scan ([[LH-576]], [[LH-234]]).

A keyed write leaves a record on the control root that answers its retries for the catalog's lifetime
(`LANCE_IDEMPOTENCY_LIFETIME_SECONDS`, a day) and then answers nothing. Maintenance's daily scan asks the catalog's
reclaim door for such records to go, and the catalog deletes them on its own reads: maintenance's store policy writes
nothing of the control root.

ONE ESTATE ON MOTO, TWO SERVICES (``catalog_and_maintenance``): the real catalog writes the record at a keyed create, and
maintenance's real scan asks its door through ``catalog_doors``. A record's age is the store's clock less the time the
store stamped on its last write, and moto stamps every write with the real clock, so the scan reads the store's clock
two days on: the one double here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pyarrow as pa
import pytest

from catalog.core.config import get_settings as catalog_settings
from lakehouse import records, stored_objects
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.idempotency import PREFIX
from tests.integration.catalog_and_maintenance import TwoServices, catalog_and_maintenance, scanned
from tests.integration.maintenance_vend_doors import CATALOG_URL, catalog_doors


KEY = "3f1d2c4b-5a69-4e7f-8a1b-2c3d4e5f6a7b"


def _records(both: TwoServices) -> list[str]:
    bucket = catalog_settings().registry_root.removeprefix("s3://").rstrip("/")
    listed = both.s3.list_objects_v2(Bucket=bucket, Prefix=f"{PREFIX}/")
    return [str(item["Key"]) for item in listed.get("Contents", [])]


def test_a_record_past_its_lifetime_is_gone_after_the_daily_scan(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    env = {"LANCE_WAREHOUSES_ENABLED": "false", "MAINTENANCE_WAREHOUSES_ENABLED": "false", "MAINTENANCE_CATALOG_URL": CATALOG_URL}
    with catalog_and_maintenance(moto_url, monkeypatch, env=env) as both:
        assert both.catalog.post("/v1/namespace/db/create", json={}).status_code == 200
        created = both.catalog.post(
            "/v1/table/db$t/create",
            content=encode_arrow_stream(pa.table({"id": pa.array([1, 2], pa.int64())})),
            headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE, "Idempotency-Key": KEY},
        )
        assert created.status_code == 200, created.text
        assert len(_records(both)) == 1, "the keyed create must have left its record"
        clock = records.store_clock

        def _two_days_on(response: Any) -> datetime:  # noqa: ANN401 — the store's response, passed on
            return clock(response) + timedelta(days=2)

        with catalog_doors(both.catalog), pytest.MonkeyPatch.context() as later:
            later.setattr(records, "store_clock", _two_days_on)
            later.setattr(stored_objects, "store_clock", _two_days_on)
            scan = scanned(both.maintenance)

        assert _records(both) == []
        assert scan["idempotency_keys"] == {"reclaimed": 1, "kept": 0, "left": 0, "unreadable": 0}
