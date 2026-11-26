"""The drift report reads the project, warehouse and trash registries through the registries' one listing
(`lakehouse.records.record_store.read_listing`), on a real S3 store: a record deleted between the listing that names it
and its read is no record and leaves the scan whole, a read the store does not answer leaves the registry unread, and
a record that is no JSON leaves the scan incomplete. Each of the last two blocks the purge the report gates
(`purge.report_is_clean`); the first must not, since a trash record purged or undropped mid-scan is the estate working.
"""

import json
import uuid

import boto3
import pytest
from pydantic import SecretStr
from tests.integration.store_requests import StoreRequest, counted

from lakehouse.storage.credential import S3KeyPair
from lakehouse.storage.objectfs import lance_storage_options
from lakehouse.storage.options import StorageOptions
from maintenance.services import reconcile
from maintenance.services.reconcile import IncompleteScan, Sources


def _control_root(moto_url: str, **records: bytes) -> str:
    """A bucket of its own holding one project record per name, each with the body given; answers its control root."""
    bucket = f"registry-{uuid.uuid4().hex[:8]}"
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    s3.create_bucket(Bucket=bucket)
    for name, body in records.items():
        s3.put_object(Bucket=bucket, Key=f"_projects/{name}.json", Body=body)
    return f"s3://{bucket}"


def _project(name: str) -> bytes:
    return json.dumps({"id": name, "created_at": "2026-11-26T00:00:00+00:00", "created_by": "someone"}).encode()


def _options(store_url: str) -> StorageOptions:
    return lance_storage_options(store_url, S3KeyPair(access_key_id="test", secret_access_key=SecretStr("test")), "us-east-1")


def _reads_b(request: StoreRequest) -> bool:
    """Whether a request reads the record ``b``: its HEAD or its GET, never the listing that names it."""
    return request.method in ("HEAD", "GET") and request.path.endswith("/_projects/b.json")


async def _loaded(control_root: str, options: StorageOptions) -> Sources:
    return await reconcile.load_sources(grants=None, control_root=control_root, namespace_root=control_root, storage_options=options, bucket_client=None)


def _projects(sources: Sources) -> list[str] | None:
    return None if sources.project_records is None else sorted(record.id for record in sources.project_records)


@pytest.mark.asyncio
async def test_a_record_deleted_after_the_listing_named_it_leaves_the_scan_whole(moto_url: str) -> None:
    control_root = _control_root(moto_url, a=_project("a"), b=_project("b"), c=_project("c"))

    with counted(moto_url) as store, store.answering_absent(_reads_b):
        sources = await _loaded(control_root, _options(store.url))

    assert (_projects(sources), sources.project_records_error, sources.incomplete) == (["a", "c"], None, [])


@pytest.mark.asyncio
async def test_a_record_the_store_does_not_answer_leaves_the_registry_unread(moto_url: str) -> None:
    control_root = _control_root(moto_url, a=_project("a"), b=_project("b"), c=_project("c"))

    with counted(moto_url) as store, store.answering_unavailable(_reads_b):
        sources = await _loaded(control_root, _options(store.url))

    assert _projects(sources) is None
    assert (sources.project_records_error or "").startswith("registry:projects: OSError")


@pytest.mark.asyncio
async def test_a_record_that_is_no_json_leaves_the_scan_incomplete(moto_url: str) -> None:
    control_root = _control_root(moto_url, a=_project("a"), b=b"{not json", c=_project("c"))

    sources = await _loaded(control_root, _options(moto_url))

    unread = IncompleteScan(source="registry:projects", reason=f"{control_root.removeprefix('s3://')}/_projects/b.json cannot be read as a record")
    assert (_projects(sources), sources.project_records_error, sources.incomplete) == (["a", "c"], None, [unread])
