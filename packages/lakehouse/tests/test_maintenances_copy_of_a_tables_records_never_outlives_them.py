"""Maintenance's copy of a table's commit records ends with them, even when the table ends while a pass reads them.

A pass lists the table's records, reads its copy and writes the copy again (``file_identities.recorded_and_copied``); the
table's end (``file_identities.end``) deletes the records and then the copy. An end landing between the pass's reads and
its write left the pass writing ``_maintenance_files/<uuid>.json`` back for a table that is gone, which nothing ends
again, since nothing names that uuid once its table ended. The write is conditional on the copy the pass read (its ETag,
or its absence), and a copy written where none was is taken back once the records it copied are found gone.

Over a moto server: the conditional puts are S3's, and an end is landed in the window by the test.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import boto3
import pytest
from moto.server import ThreadedMotoServer
from pydantic import SecretStr

from lakehouse import file_identities
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.objectfs import lance_storage_options
from lakehouse.storage_options import StorageOptions
from lakehouse.stored_objects import ObjectIdentity, list_under


@pytest.fixture(scope="module")
def moto_url() -> Iterator[str]:
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


def _committed(transaction: str) -> file_identities.CommittedFiles:
    identity = ObjectIdentity(etag="0f343b0931126a20f133d67c2b018a3b", size=7)
    return file_identities.CommittedFiles(
        transaction_uuid=transaction, files=[file_identities.CommittedFile(path=f"data/{transaction}.lance", identity=identity)]
    )


@pytest.mark.parametrize("copied_before", [pytest.param(False, id="no-copy-yet"), pytest.param(True, id="a-copy-read")])
def test_a_table_ended_inside_a_pass_keeps_no_copy_of_its_records(moto_url: str, monkeypatch: pytest.MonkeyPatch, copied_before: bool) -> None:
    bucket = f"ctl-{uuid.uuid4().hex[:8]}"
    boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1").create_bucket(Bucket=bucket)
    control = f"s3://{bucket}"
    options: StorageOptions = lance_storage_options(moto_url, S3KeyPair(access_key_id="test", secret_access_key=SecretStr("test")), "us-east-1")
    table = str(uuid.uuid4())
    file_identities.record(control, options, table, _committed(str(uuid.uuid4())))
    if copied_before:
        file_identities.recorded_and_copied(control, options, table)
        file_identities.record(control, options, table, _committed(str(uuid.uuid4())))
    read = file_identities._read_record

    def _ended_once_read(control_root: str, storage_options: StorageOptions, key: str) -> file_identities.CommittedFiles | None:
        found = read(control_root, storage_options, key)
        file_identities.end(control_root, storage_options, table)
        return found

    # The pass's last read of a record, and then the table's end, before the pass writes its copy.
    monkeypatch.setattr(file_identities, "_read_record", _ended_once_read)

    file_identities.recorded_and_copied(control, options, table)

    left = [stored.path for prefix in (file_identities.PREFIX, file_identities.COPY_PREFIX) for stored in list_under(f"{control}/{prefix}", options)]
    assert left == [], left
