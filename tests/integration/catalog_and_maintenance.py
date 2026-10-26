"""The real catalog and the real maintenance service over one control root on moto, asking one in-memory OpenFGA.

For a test whose claim crosses the two services: the catalog writes what a door leaves (a trash record, a grant) and a
maintenance tick acts on it. The catalog runs with OIDC and FGA on once its lifespan has run, acting as alice, and the
``TupleStore`` behind both holds what the doors write (``openfga_grants``).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import boto3
import httpx
import pytest
from fastapi.testclient import TestClient

from catalog.core.config import get_settings as catalog_settings
from catalog.core.lineage_emit import HttpLineageEmitter
from maintenance.core.config import get_settings as maintenance_settings
from storage import S3Client
from tests.integration.openfga_grants import TupleStore, act_as


class TwoServices:
    """The two apps' clients, the store behind both, and an S3 client on the control bucket's store."""

    def __init__(self, catalog: TestClient, maintenance: TestClient, store: TupleStore, s3: S3Client) -> None:
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
    maintenance_fga: bool = False,
    control: Any = None,  # noqa: ANN401 — the maintenance app's ControlEmitter, as the test captures it
) -> Generator[TwoServices]:
    """Both apps over a fresh control bucket, with ``env`` over the shared settings and project-less.

    ``maintenance_fga`` turns FGA on in maintenance's settings, as the chart does; ``control`` replaces its control
    emitter.
    """
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
        "LANCE_WAREHOUSES_ENABLED": "true",
        "MAINTENANCE_S3_ENDPOINT": moto_url,
        "MAINTENANCE_S3_ACCESS_KEY_ID": "test",
        "MAINTENANCE_S3_SECRET_ACCESS_KEY": "test",
        "MAINTENANCE_S3_BUCKET": bucket,
        "MAINTENANCE_CONTROL_ROOT": f"s3://{bucket}",
        "MAINTENANCE_WAREHOUSES_ENABLED": "true",
        **env,
    }.items():
        monkeypatch.setenv(key, value)
    catalog_settings.cache_clear()
    maintenance_settings.cache_clear()
    from catalog.main import app as catalog_app
    from maintenance.service import app as maintenance_app

    store = TupleStore()
    lineage = HttpLineageEmitter(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))), "http://lineage/api/v1/lineage", job_namespace="lance-catalog"
    )
    try:
        with TestClient(catalog_app, headers={"Authorization": "Bearer t"}) as catalog, TestClient(maintenance_app) as maintenance:
            governed = catalog_settings().model_copy(
                update={
                    "oidc_enabled": True,
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
            monkeypatch.setattr(catalog_app.state, "lineage_emitter", lineage, raising=False)
            monkeypatch.setattr(maintenance_app.state, "fga_client", store)
            if maintenance_fga:
                maintained = maintenance_settings().model_copy(update={"fga_enabled": True})
                maintenance_app.dependency_overrides[maintenance_settings] = lambda: maintained
            if control is not None:
                monkeypatch.setattr(maintenance_app.state, "control_emitter", control, raising=False)
            yield TwoServices(catalog, maintenance, store, s3)
    finally:
        catalog_app.dependency_overrides.clear()
        maintenance_app.dependency_overrides.clear()
        catalog_settings.cache_clear()
        maintenance_settings.cache_clear()
