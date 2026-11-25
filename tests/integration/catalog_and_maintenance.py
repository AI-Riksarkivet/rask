"""The real catalog and maintenance's worker resources over one control root on moto, asking one in-memory OpenFGA.

For a test whose claim crosses the two services: the catalog writes what a door leaves (a trash record, a grant) and a
maintenance audit acts on it, through the catalog's own doors (`audited`), on a loop that holds what the worker's start
holds before it takes a task (`maintenance.runtime.standing`). The catalog acts as alice with OIDC and FGA on: given a real
OpenFGA store and the loopback Dex, as deployed, alice administering the estate and holding the rungs maintenance's
doors ask; otherwise once its lifespan has run, over a ``TupleStore`` that holds what the doors write and allows every
check (``openfga_grants``), for a test whose claim is not who may act.
"""

import json
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from functools import partial
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog.core.config import get_settings as catalog_settings
from catalog.core.lineage_emit import BusEmitter
from lakehouse.process.supervision import Supervisor
from lakehouse.storage.client import S3Client
from maintenance.core.config import get_settings as maintenance_settings
from maintenance.runtime import standing
from tests.bus_double import BusDouble
from tests.governed_catalog import GovernedCatalog, StoreTuples
from tests.integration.openfga_grants import TupleStore, act_as


@asynccontextmanager
async def _standing(app: FastAPI) -> AsyncGenerator[None]:
    """What maintenance's worker stands on, started as its start starts it (`maintenance.worker`): the secret store's
    login and the object store's session under its settings, their loops supervised, stopped on the way out."""
    async with Supervisor() as loops, standing(app, maintenance_settings(), loops):
        yield
        await loops.stop()


class TwoServices:
    """The catalog's client, a client whose loop runs on what maintenance's worker stands on, the store behind both,
    and an S3 client on the control bucket's store."""

    def __init__(self, catalog: TestClient, maintenance: TestClient, store: TupleStore | StoreTuples, s3: S3Client) -> None:
        self.catalog = catalog
        self.maintenance = maintenance
        self.store = store
        self.s3 = s3


@contextmanager
def catalog_and_maintenance(
    moto_url: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    env: dict[str, str],
    catalog_control: Any = None,  # noqa: ANN401 — the catalog app's ControlEmitter, as the test captures it
    openfga_store: Any = None,  # noqa: ANN401 — the root conftest's OpenFgaStore
    dex_issuer: Any = None,  # noqa: ANN401 — the root conftest's DexIssuer
) -> Generator[TwoServices]:
    """The catalog and maintenance's standing resources over a fresh control bucket, with ``env`` over the shared
    settings and project-less.

    ``catalog_control`` replaces the catalog's control emitter. Maintenance holds no OpenFGA client: it
    asks the catalog's doors, which a test routes to this catalog (`maintenance_vend_doors.catalog_doors`), so they are
    asked as alice. With ``openfga_store`` and ``dex_issuer`` the catalog decides on that store as deployed, and alice
    holds the estate's ``admin``, and the ``reclaimer`` and ``reconciler`` rungs those doors ask.
    """
    governed_env: dict[str, str] = {}
    if openfga_store is not None:
        governed_env = {
            "RASK_OIDC_ENABLED": "true",
            "RASK_OIDC_ISSUER": dex_issuer.issuer,
            "RASK_OIDC_AUDIENCE": "lance-catalog",
            "RASK_OIDC_ALLOW_INSECURE": "true",
            "RASK_OIDC_IDP_ID": GovernedCatalog.IDP,
            "RASK_OIDC_CONNECTORS": json.dumps({GovernedCatalog.CONNECTOR: GovernedCatalog.IDP}),
            "RASK_FGA_ENABLED": "true",
            "RASK_FGA_API_URL": openfga_store.url,
            "RASK_FGA_STORE_ID": openfga_store.store_id,
        }
        alice = f"user:{GovernedCatalog.IDP}~alice"
        openfga_store.write(*((alice, rung, "estate:rask") for rung in ("admin", "reclaimer", "reconciler")))
    bucket = f"two-{uuid.uuid4().hex[:10]}"
    s3: S3Client = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    s3.create_bucket(Bucket=bucket)
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "MAINTENANCE_S3_ENDPOINT": moto_url,
        "MAINTENANCE_S3_ACCESS_KEY_ID": "test",
        "MAINTENANCE_S3_SECRET_ACCESS_KEY": "test",
        "MAINTENANCE_S3_BUCKET": bucket,
        "MAINTENANCE_CONTROL_ROOT": f"s3://{bucket}",
        **governed_env,
        **env,
    }.items():
        monkeypatch.setenv(key, value)
    catalog_settings.cache_clear()
    maintenance_settings.cache_clear()
    from catalog.main import app as catalog_app

    maintenance_app = FastAPI(lifespan=_standing)
    lineage = BusEmitter(
        BusDouble(), "lineage.events.v1", job_namespace="lance-catalog", dataset_namespace="lance-namespace://localhost/api/catalog", timeout_seconds=5.0
    )
    bearer = f"Bearer {dex_issuer.mint('alice', connector=GovernedCatalog.CONNECTOR, audience='lance-catalog')}" if openfga_store is not None else "Bearer t"
    try:
        with TestClient(catalog_app, headers={"Authorization": bearer}) as catalog, TestClient(maintenance_app) as maintenance:
            monkeypatch.setattr(catalog_app.state, "lineage_emitter", lineage, raising=False)
            if catalog_control is not None:
                monkeypatch.setattr(catalog_app.state, "control_emitter", catalog_control, raising=False)
            if openfga_store is not None:
                yield TwoServices(catalog, maintenance, StoreTuples(openfga_store), s3)
                return
            store = TupleStore()
            governed = catalog_settings().model_copy(
                update={
                    "oidc_enabled": True,
                    "oidc_idp_id": "dex",
                    "oidc_connectors": {"local": "dex"},
                    "oidc_issuer": "https://idp.example",
                    "oidc_audience": "lance",
                    "fga_enabled": True,
                }
            )
            catalog_app.dependency_overrides[catalog_settings] = lambda: governed
            monkeypatch.setattr(catalog_app.state, "oidc", MagicMock())
            act_as(catalog, "alice")
            monkeypatch.setattr(catalog_app.state, "fga", store)
            yield TwoServices(catalog, maintenance, store, s3)
    finally:
        catalog_app.dependency_overrides.clear()
        catalog_settings.cache_clear()
        maintenance_settings.cache_clear()


def audited(maintenance: TestClient) -> dict[str, Any]:
    """One hourly audit on the loop maintenance's standing resources run on, under its settings, as the worker runs the
    audit's task (`maintenance.services.tasks.run_task`); the payload it logs."""
    from maintenance.services.audit import run_audit

    portal = maintenance.portal
    assert portal is not None, "the client was never entered, so what the worker stands on was never started"
    return portal.call(partial(run_audit, maintenance_settings(), queue=None))


def scanned(maintenance: TestClient) -> dict[str, Any]:
    """One daily scan on the loop maintenance's standing resources run on, under its settings, as the worker runs the
    scan's task; the payload it logs."""
    from lakehouse.events.control_emit import NoopControlEmitter
    from maintenance.services.audit import run_scan

    portal = maintenance.portal
    assert portal is not None, "the client was never entered, so what the worker stands on was never started"
    return portal.call(partial(run_scan, maintenance_settings(), control=NoopControlEmitter()))
