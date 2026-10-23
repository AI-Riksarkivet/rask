"""The table-maintenance job compacts every due table it may maintain, and nothing else ([[LH-529]], ADR 0162).

The job is a bring-your-own client. It reads the catalog's due list as its own service-account principal, and the list
names only the tables it holds ``can_maintain`` on; it then runs Lance's own compaction of each through the catalog,
which judges both of the compaction's commits at ``version/create``, where a maintainer's rung is what admits them.

Driven against the real catalog under uvicorn on a moto S3 server, every caller verified by a loopback issuer and every
decision OpenFGA's on a store of the test's own. The due list and the clone list are written as the maintenance sweep
writes them. The catalog vends no credential here (Mode B), so the job's stock client signs with the store key its
environment holds; which tier a maintainer is vended is the spec doors' own suite's claim.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import httpx
import lance
import pyarrow as pa
import pytest
import uvicorn
from fastapi import FastAPI
from moto.server import ThreadedMotoServer
from pydantic import BaseModel, ConfigDict

from service_kit.governed import fga
from service_kit.lakehouse import base_refs, maintenance_due
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from table_maintenance.job import JobSettings, run


#: The job's service account, in the namespace a release of its own runs it in.
_SA, _SA_NAMESPACE = "table-maintenance", "rask"
_JOB = f"kubernetes~system:serviceaccount:{_SA_NAMESPACE}:{_SA}"
_ARROW = {"Content-Type": ARROW_STREAM_MEDIA_TYPE}
_TABLES = ("acme$kept", "acme$other")


class Estate(BaseModel):
    """The served catalog, the job's settings, and the store's own address and key, for reading what landed."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str
    settings: JobSettings
    storage_options: dict[str, str]
    locations: dict[str, str]
    uuids: dict[str, str]


def _serve(app: FastAPI) -> Iterator[str]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(timeout=30)


def _rows(start: int) -> bytes:
    return encode_arrow_stream(pa.table({"id": pa.array(range(start, start + 2), pa.int64())}))


@pytest.fixture
def estate(
    sa_issuer: Any,
    dex_issuer: Any,
    openfga_store: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Estate]:
    """The root conftest's issuers and OpenFGA store, a moto S3 bucket, and the catalog served over them."""
    moto = ThreadedMotoServer(port=0)
    moto.start()
    host, port = moto.get_host_and_port()
    endpoint = f"http://{host}:{port}"
    bucket = f"lh529-{uuid.uuid4().hex[:10]}"
    boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test").create_bucket(Bucket=bucket)
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": endpoint,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": dex_issuer.issuer,
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_OIDC_ALLOW_INSECURE": "true",
        "RASK_OIDC_CONNECTORS": json.dumps({"local": "dex"}),
        "RASK_SA_ISSUER": sa_issuer.issuer,
        "RASK_SA_AUDIENCE": "rask-catalog",
        "RASK_SA_FETCH_TOKEN_FILE": str(sa_issuer.fetch_token_file),
        "RASK_SA_CA_FILE": str(sa_issuer.ca_file),
        "RASK_FGA_ENABLED": "true",
        "RASK_FGA_API_URL": openfga_store.url,
        "RASK_FGA_STORE_ID": openfga_store.store_id,
        "RASK_FGA_MODEL_ID": openfga_store.model_id,
        # The store key the job's stock client signs with, where the catalog vends none.
        "AWS_ENDPOINT": endpoint,
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_REGION": "us-east-1",
        "AWS_ALLOW_HTTP": "true",
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    token_file = tmp_path / "projected-rask-catalog-token"
    token_file.write_text(sa_issuer.mint(_SA, audience="rask-catalog", namespace=_SA_NAMESPACE, ttl=3600))
    alice = {"Authorization": f"Bearer {dex_issuer.mint('alice', connector='local', audience='lance-catalog')}"}
    for url in _serve(app):
        with httpx.Client(base_url=url, headers=alice, timeout=60) as catalog:
            assert catalog.post("/v1/namespace/acme/create", json={}).status_code == 200
            for table in _TABLES:
                created = catalog.post(f"/v1/table/{table}/create", content=_rows(0), headers=_ARROW)
                assert created.status_code == 200, created.text
                for start in (2, 4):
                    assert catalog.post(f"/v1/table/{table}/insert", content=_rows(start), headers=_ARROW).status_code == 200
            described = {table: catalog.post(f"/v1/table/{table}/describe", json={"vend_credentials": False}).json() for table in _TABLES}
        locations = {table: str(answer["location"]) for table, answer in described.items()}
        uuids = {table: str(answer["properties"]["rask.uuid"]) for table, answer in described.items()}
        # The rungs a compaction needs, on the one table the job maintains, as a grant door or the catalog's boot writes them.
        openfga_store.write(*((fga.user_object(_JOB), rung, fga.table_object(uuids["acme$kept"])) for rung in ("reader", "maintainer")))
        settings = get_settings()
        for table in _TABLES:
            due = maintenance_due.DueRecord(
                table=table,
                uuid=uuids[table],
                location=locations[table],
                version=3,
                compaction=maintenance_due.CompactionDue(fragments=3, candidate_fragments=3, tasks=1),
                evaluated_at=datetime.now(UTC),
            )
            maintenance_due.write_due(settings.registry_root, settings.storage_options(), due)
        clones = maintenance_due.CloneList.taken(base_refs.BaseRefs(), at=datetime.now(UTC))
        maintenance_due.write_clone_list(settings.registry_root, settings.storage_options(), clones)
        own = {"aws_endpoint": endpoint, "aws_region": "us-east-1", "allow_http": "true", "aws_access_key_id": "test", "aws_secret_access_key": "test"}
        yield Estate(url=url, settings=JobSettings(catalog_url=url, token_file=token_file), storage_options=own, locations=locations, uuids=uuids)
    get_settings.cache_clear()
    moto.stop()


def test_the_job_compacts_every_due_table_it_may_maintain_and_nothing_else(estate: Estate) -> None:
    ran = run(estate.settings)

    assert (ran.compacted, ran.failed) == (["acme$kept"], {})
    kept = lance.dataset(estate.locations["acme$kept"], storage_options=estate.storage_options)
    assert (kept.version, len(kept.get_fragments()), sorted(kept.to_table()["id"].to_pylist())) == (5, 1, list(range(6)))
    other = lance.dataset(estate.locations["acme$other"], storage_options=estate.storage_options)
    assert (other.version, len(other.get_fragments())) == (3, 3)
