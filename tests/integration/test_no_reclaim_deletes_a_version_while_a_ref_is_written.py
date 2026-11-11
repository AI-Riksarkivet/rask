"""[[LH-308]] No reclaim deletes a table's versions while one of its tags or branches is being written.

Lance's ``cleanup_old_versions`` reads the refs once, when it starts (``rust/lance/src/dataset/cleanup.rs:472-511`` at
v13.0.0), so a tag or branch written while it runs can name a version it then deletes. Measured on pylance 13.0.0, a
tag created on version 2 a few milliseconds into ``cleanup_old_versions(older_than=0)`` named a deleted version in 20 of
60 races, and a branch could not be read in 9 of 60. A ref write and a reclaim therefore each hold the table's history
fence (:mod:`lakehouse.history_fences`); the ref doors' side is pinned in
``services/catalog/tests/test_tag_and_branch_failures_carry_their_spec_code.py``.

One estate on moto: the catalog over HTTP and maintenance's own pass. While a ref write holds the table's history,
every reclaim path deletes nothing: the catalog's ``maintenance/run`` and ``version/delete`` answer 409 code 14, and
the sweep's pass over the table is skipped this tick and reclaims on the next. Once the pass holds the history, it reads
the trash again, so a table dropped since its pass began is not reclaimed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import boto3
import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from lakehouse import maintenance_due, maintenance_policies, trash
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.base_refs import normalise
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.history_fences import HistoryWork, TableHistory
from lakehouse.naming import UUID_PROPERTY
from lakehouse.objectfs import lance_storage_options
from maintenance.core.config import MaintenanceSettings
from maintenance.services import table_pass
from maintenance.services.due import TableIds
from maintenance.services.expiry import DatasetResult
from maintenance.services.naming import claim_store, policy_owners
from maintenance.services.plan import PlanContext
from tests.integration.maintenance_vend_doors import CATALOG_URL, described_by, maintain_tier_door


TABLE = "db$t"


class _Estate:
    """The catalog on a fresh bucket that is also its control root, holding one table with three versions."""

    def __init__(self, url: str, client: TestClient, bucket: str) -> None:
        self.url = url
        self.client = client
        self.root = f"s3://{bucket}"
        self.key = lance_storage_options(url, S3KeyPair(access_key_id="test", secret_access_key=SecretStr("test")), "us-east-1")
        assert client.post("/v1/namespace/db/create", json={}).status_code == 200
        for start, mode in ((0, None), (3, "overwrite"), (6, "overwrite")):
            params = {"mode": mode} if mode else {}
            rows = encode_arrow_stream(pa.table({"id": pa.array(range(start, start + 3), pa.int64())}))
            answer = client.post(f"/v1/table/{quote(TABLE, safe='$')}/create", params=params, content=rows, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
            assert answer.status_code == 200, answer.text
            self.location = str(answer.json()["location"])
        described = client.post(f"/v1/table/{quote(TABLE, safe='$')}/describe", json={})
        assert described.status_code == 200, described.text
        self.table_uuid = str(described.json()["properties"][UUID_PROPERTY])

    def versions(self) -> list[int]:
        return [int(version["version"]) for version in lance.dataset(self.location, storage_options=self.key.in_clear()).versions()]

    def swept(self, policies: list[dict[str, object]] | None = None) -> DatasetResult:
        """Maintenance's own pass over the table under ``policies``, every version but the current one reclaimable."""
        now = datetime.now(UTC)
        namespace = self.client.post("/v1/namespace/db/describe", json={})
        assert namespace.status_code == 200, namespace.text
        settings = MaintenanceSettings.model_validate(
            {
                "s3_endpoint": self.url,
                "s3_access_key_id": "test",
                "s3_secret_access_key": "test",
                "s3_bucket": self.root.removeprefix("s3://"),
                "policy_root": self.root,
                "control_root": self.root,
                "min_versions_to_expire": 1,
                "catalog_url": CATALOG_URL,
            }
        )
        ids = TableIds(by_location={normalise(self.location): TABLE}, namespace_uuids={"db": str(namespace.json()["properties"][UUID_PROPERTY])})
        readings = table_pass.PassReadings(
            table_ids=ids,
            context=PlanContext(policies=list(policies or []), now=now, older_than=timedelta(0)),
            table=TABLE,
            owners=policy_owners(ids, TABLE, claims=claim_store(settings)),
        )
        with maintain_tier_door(described_by(self.client)):
            return table_pass.maintain_dataset(self.location, readings=readings, settings=settings)


@pytest.fixture
def estate(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Estate]:
    bucket = f"lh308-{uuid.uuid4().hex[:10]}"
    boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1").create_bucket(Bucket=bucket)
    for name, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
    }.items():
        monkeypatch.setenv(name, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        yield _Estate(moto_url, client, bucket)
    get_settings.cache_clear()


@pytest.mark.parametrize("reclaim", ["maintenance/run", "version/delete", "the-sweep"])
def test_no_reclaim_deletes_a_version_while_a_ref_write_holds_the_tables_history(estate: _Estate, reclaim: str) -> None:
    versions = estate.versions()
    assert versions == [1, 2, 3], "precondition: a create and two overwrites"
    TableHistory(control_root=estate.root, storage_options=estate.key, table_uuid=estate.table_uuid).take(HistoryWork.REFS)

    match reclaim:
        case "maintenance/run":
            answer = estate.client.post(f"/management/v1/table/{quote(TABLE, safe='$')}/maintenance/run", json={"retain_versions": 1})
            assert (answer.status_code, answer.json().get("code")) == (409, 14), answer.text
        case "version/delete":
            answer = estate.client.post(f"/v1/table/{quote(TABLE, safe='$')}/version/delete", json={"ranges": [{"start_version": 1, "end_version": 3}]})
            assert (answer.status_code, answer.json().get("code")) == (409, 14), answer.text
        case _:
            result = estate.swept()
            assert (result.skipped, result.error, result.old_versions_removed) == ("history_fenced", None, 0), result

    assert estate.versions() == versions, f"{reclaim} deleted versions while a ref write held the table's history"


def test_a_table_dropped_after_its_pass_read_the_trash_is_not_reclaimed(estate: _Estate) -> None:
    """A recoverable drop can land after the table's pass read the trash, its vend already in hand: the pass reads the
    trash again once it holds the table's history, reclaims nothing from a table the trash now keeps, and leaves it as a
    table the trash keeps when its pass starts is left: off the due list, its policy's cadence unstamped."""
    for start in (10, 20):
        rows = encode_arrow_stream(pa.table({"id": pa.array(range(start, start + 3), pa.int64())}))
        answer = estate.client.post(f"/v1/table/{quote(TABLE, safe='$')}/insert", content=rows, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
        assert answer.status_code == 200, answer.text
    versions = estate.versions()
    policy: dict[str, object] = {"kind": "table", "id": TABLE, "uuid": estate.table_uuid, "compact_enabled": True, "compact_interval_hours": 24}
    stale = maintenance_due.DueRecord(table=TABLE, uuid=estate.table_uuid, location=estate.location, version=1, evaluated_at=datetime.now(UTC))
    maintenance_due.write_due(estate.root, estate.key, stale)
    record = trash.make_record(TABLE, uuid=estate.table_uuid, location=estate.location, dropped_by=None, grace_days=7)
    trash.put(estate.root, estate.key, record)

    result = estate.swept([policy])

    assert (result.trashed, result.error, result.old_versions_removed, estate.versions()) == (estate.table_uuid, None, 0, versions)
    stamped = maintenance_policies.read_state(estate.root, estate.key, policy, table_uuid=estate.table_uuid, uri=estate.location)
    assert (maintenance_due.read_due(estate.root, estate.key, estate.table_uuid), stamped) == (None, None)
