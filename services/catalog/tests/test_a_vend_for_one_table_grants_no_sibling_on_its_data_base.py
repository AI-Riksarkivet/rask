"""[[LH-252]] One table on a data base reaches nothing under another table's directory there.

A table registered at the approved base itself shares one flat ``<base>/`` with every other table
naming it, and a session policy granting that base grants READ on every sibling's fragments for the
credential's lifetime. So each table registers its own directory, ``<base>/<directory>/``, the policy
grants exactly the directories the table's manifest declares, and a directory is HELD by its table: a
registration whose manifest declares another table's directory is refused, or it would be vended GET on
that table's fragments.

Driven through the real create, register and credentials doors on the two-store estate
(``conftest.two_store_catalog``), on the approved data base that carries no credential reference — a
base with one is never vended directly ([[LH-273]]). moto's S3 does not enforce a session policy, so the
policy the lifespan-built ``StsVendor`` hands to AssumeRole is judged by moto's IAM evaluator
(``IAMPolicy``) against the object keys each table's fragments occupy. Whether MinIO's STS enforces it
is the live probe's question.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import boto3
import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient
from moto.iam.access_control import IAMPolicy, PermissionResult

from service_kit.lakehouse.features import manifest_base_path_refs


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _ids(*ids: int) -> pa.Table:
    return pa.table({"id": pa.array(list(ids), pa.int64())})


def _create(client: TestClient, base: str, rows: pa.Table) -> tuple[str, str]:
    table = f"mb${uuid.uuid4().hex[:12]}"
    created = client.post(f"/v1/table/{table}/create?data_base={base}", content=_ipc(rows), headers=ARROW)
    assert created.status_code == 200, created.text
    return table, str(created.json()["location"])


def _options(two_stores: Any) -> dict[str, str]:  # noqa: ANN401 — conftest.TwoStores
    return {"endpoint": two_stores.url, "allow_http": "true", "region": "us-east-1", **two_stores.estate.model_dump()}


def _directory(two_stores: Any, location: str) -> str:  # noqa: ANN401
    (base,) = [ref.path for ref in manifest_base_path_refs(lance.dataset(location, storage_options=_options(two_stores)))]
    return base


def _fragment_keys(two_stores: Any, directory: str) -> list[tuple[str, str]]:  # noqa: ANN401
    bucket, _, prefix = directory.removeprefix("s3://").partition("/")
    s3 = boto3.client("s3", endpoint_url=two_stores.url, region_name="us-east-1", **two_stores.estate.model_dump())  # noqa: TID251 — a test reading moto
    keys = [(bucket, item["Key"]) for item in s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/").get("Contents", [])]
    assert keys, f"precondition: the table's fragments are under its data directory {directory}"
    return keys


def _permits_get(policy: dict[str, Any], bucket: str, key: str) -> bool:
    """IAM's verdict on GetObject over ``bucket/key``, by moto's evaluator.

    moto turns a Resource into a regex without escaping it, so the ``$`` a table directory carries would
    read as an end anchor; it is swapped for a character IAM gives no meaning, on both sides alike.
    """
    document = json.dumps(policy).replace("$", "~")
    return IAMPolicy(document).is_action_permitted("s3:GetObject", f"arn:aws:s3:::{bucket}/{key.replace('$', '~')}") == PermissionResult.PERMITTED


def _capture_policy(client: TestClient) -> dict[str, Any]:
    sent: dict[str, Any] = {}

    def _assume_role(**kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        sent["policy"] = json.loads(str(kwargs["Policy"]))
        return {"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "ST", "Expiration": None}}

    client.app.state.vendor._assume_role = _assume_role  # noqa: SLF001 — the one call that would reach the store
    return sent


@pytest.mark.parametrize("second_table", ["created", "registered-over-its-directory"])
def test_a_table_on_a_data_base_reaches_none_of_another_tables_fragments(
    two_store_catalog: TestClient,
    two_stores: Any,
    plain_base: str,
    second_table: str,  # noqa: ANN401
) -> None:
    owner, owner_location = _create(two_store_catalog, plain_base, _ids(1, 2))
    owner_keys = _fragment_keys(two_stores, _directory(two_stores, owner_location))
    sent = _capture_policy(two_store_catalog)

    if second_table == "created":
        sibling, sibling_location = _create(two_store_catalog, plain_base, _ids(3, 4))
        vended = two_store_catalog.post(f"/management/v1/table/{sibling}/credentials")

        assert vended.status_code == 200, vended.text
        assert vended.json()["mode"] == "direct", vended.text
        sibling_keys = _fragment_keys(two_stores, _directory(two_stores, sibling_location))
        assert all(_permits_get(sent["policy"], *key) for key in sibling_keys), f"the sibling's vend cannot read its own fragments: {sent['policy']}"
        assert not [key for key in owner_keys if _permits_get(sent["policy"], *key)], f"the sibling's vend reads the owner's fragments: {sent['policy']}"
    else:
        # The thief writes its own dataset declaring the owner's directory as a base, as any holder of a
        # write credential on its own prefix can, then asks the catalog to register it.
        thief_location = f"thief-{uuid.uuid4().hex[:8]}"
        lance.write_dataset(
            _ids(9),
            f"s3://{two_stores.estate_bucket}/{thief_location}",
            storage_options=_options(two_stores),
            data_storage_version="2.2",
            enable_stable_row_ids=True,
            initial_bases=[lance.DatasetBasePath(_directory(two_stores, owner_location), is_dataset_root=False, name="data")],
        )
        registered = two_store_catalog.post(f"/v1/table/mb$thief{uuid.uuid4().hex[:8]}/register", json={"location": thief_location})

        assert registered.status_code == 400, f"a registration declaring {owner}'s data directory was admitted: {registered.text}"
        assert "holds" in registered.json()["detail"], registered.text
