"""[[LH-543]] A service's object-store credential is in clear only at the call that signs with it, so no model holding one prints it.

Every model that reaches the object store holds `service_kit.lakehouse.objectfs.StorageOptions`: the key pair as
`S3KeyPair`, its secret and session token `SecretStr`, beside every other option in clear. The flat map Lance takes
(`lance_docs/lance/guide/object_store.md:122-136`) is built only at the Lance, boto3 or pyarrow call, so a repr, a str, a
model dump, a log line or an error that echoes one of these models carries neither, as a vend carries neither
([[LH-072]]) and Lakekeeper derives `Redact` on its credential structs.

Each model is built as its service builds it: the catalog signing with the session its ServiceAccount token was exchanged
for (ADR 0170), and maintenance's expiry pass holding its own key pair in the table's history fence beside the vend it
signs with. Only the STS exchange and the catalog's vend door (with respx, at the URL maintenance asks) are played.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import pytest
import respx
from pydantic import BaseModel, SecretStr

from catalog.api import idempotency
from catalog.core.config import Settings
from catalog.services import table_claims
from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials
from maintenance.services.expiry import ExpiryTarget
from service_kit.context import DiagnosticFormatter
from service_kit.governed import store_session
from service_kit.governed.store_credential import S3KeyPair
from service_kit.lakehouse.base_registry import BaseRegistry
from service_kit.lakehouse.history_fences import TableHistory


log = logging.getLogger(__name__)

#: Distinctive on purpose: a short secret is a substring of ordinary prose, and the assertion would prove nothing.
_CATALOG_SECRET = "catalog-session-secret-4b7e"
_CATALOG_TOKEN = "catalog-session-token-9c2d"
_MAINTENANCE_SECRET = "maintenance-own-secret-1f8a"
_VEND_SECRET = "vended-secret-6d3b"
_VEND_TOKEN = "vended-token-2e5c"
_HELD = (_CATALOG_SECRET, _CATALOG_TOKEN, _MAINTENANCE_SECRET, _VEND_SECRET, _VEND_TOKEN)

_TABLE = "acme-bronze$events"
_URI = "s3://acme-bucket/4c49d010_acme-bronze$events"
_CATALOG_URL = "http://catalog:2333"
#: The table-scoped session the catalog's vend door answers maintenance with.
_VEND = {
    "mode": "direct",
    "credentials": {
        "storage_options": {
            "endpoint": "https://s3.public.example",
            "aws_access_key_id": "VENDEDKEY",
            "aws_secret_access_key": _VEND_SECRET,
            "aws_session_token": _VEND_TOKEN,
            "region": "us-east-1",
            "allow_http": "false",
        }
    },
}


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A catalog signing with the session its token was exchanged for, as the deployed one does (ADR 0170)."""
    token = tmp_path / "rask-object-store-token"
    token.write_text("projected\n")
    sessions = store_session.SessionCache()
    monkeypatch.setattr(store_session, "SESSIONS", sessions)
    settings = Settings(
        LANCE_S3_ENDPOINT="http://rustfs:9000",
        LANCE_S3_ACCESS_KEY_ID="",
        LANCE_S3_SECRET_ACCESS_KEY="",
        LANCE_S3_WEB_IDENTITY_TOKEN_FILE=str(token),
        LANCE_S3_STS_ENDPOINT="http://rustfs:9000",
        LANCE_CONTROL_ROOT=str(tmp_path / "control"),
    )
    identity = settings.web_identity
    assert identity is not None, "the catalog holds no web identity, so it would sign with no session"
    session = S3KeyPair(access_key_id="CATALOGSESSION", secret_access_key=SecretStr(_CATALOG_SECRET), session_token=SecretStr(_CATALOG_TOKEN))
    sessions.hold(identity, session, asked_at=sessions.now(), lifetime_seconds=3600)
    return settings


@pytest.fixture
def maintenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MaintenanceSettings:
    """Maintenance under its own key pair, with its catalog token projected."""
    token = tmp_path / "rask-catalog-token"
    token.write_text("sa-token\n")
    monkeypatch.setenv("RASK_CATALOG_IDENTITY_TOKEN_FILE", str(token))
    return MaintenanceSettings(
        MAINTENANCE_CATALOG_URL=_CATALOG_URL,
        MAINTENANCE_S3_ACCESS_KEY_ID="rask-maintenance",
        MAINTENANCE_S3_SECRET_ACCESS_KEY=_MAINTENANCE_SECRET,
    )


class _Rig(NamedTuple):
    """Both services' settings, and the router the catalog's vend door is played on."""

    catalog: Settings
    maintenance: MaintenanceSettings
    door: respx.MockRouter


@pytest.fixture
def rig(catalog: Settings, maintenance: MaintenanceSettings, respx_mock: respx.MockRouter) -> _Rig:
    return _Rig(catalog=catalog, maintenance=maintenance, door=respx_mock)


def _claim_store(rig: _Rig) -> BaseModel:
    return table_claims.claim_store(rig.catalog)


def _base_registry(rig: _Rig) -> BaseModel:
    """As the table doors build it."""
    return BaseRegistry(control_root=rig.catalog.registry_root, storage_options=rig.catalog.storage_options())


def _keyed_door(rig: _Rig) -> BaseModel:
    """As `idempotency.keyed_door` builds it, holding the claim `idempotency.begin` answers a keyless request with."""
    options = rig.catalog.storage_options(plane="data")
    converge = asyncio.run(idempotency.begin(rig.catalog.registry_root, options, "caller", None, endpoint="create_table"))
    return idempotency.KeyedDoor(registry_root=rig.catalog.registry_root, storage_options=options, scope="caller", key=None, converge=converge)


def _expiry_target(rig: _Rig) -> BaseModel:
    """As the sweep hands it to the expiry pass: its own key pair in the history fence, and the vend the pass signs with."""
    rig.door.post(f"{_CATALOG_URL}/management/v1/table/{_TABLE}/credentials", params={"tier": "maintain"}).mock(return_value=httpx.Response(200, json=_VEND))
    options = rig.maintenance.storage_options()
    history = TableHistory(control_root=rig.maintenance.resolved_control_root, storage_options=options, table_uuid="0b6f8a52-4c1e-4f7a-9d3e-2a1c5b7e9f01")
    target = ExpiryTarget(uri=_URI, storage_options=options, table_id=_TABLE, history=history)
    target.storage_options = credentials.write_options_for(_URI, rig.maintenance, table_id=_TABLE, fallback=options)
    return target


@pytest.mark.parametrize(
    "holder",
    [
        pytest.param(_claim_store, id="catalog claim store"),
        pytest.param(_base_registry, id="catalog base registry"),
        pytest.param(_keyed_door, id="catalog keyed door"),
        pytest.param(_expiry_target, id="maintenance expiry target"),
    ],
)
def test_a_model_holding_a_store_credential_renders_neither_its_secret_nor_its_session_token(
    holder: Callable[[_Rig], BaseModel], rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """Every way a service turns an object into text, short of the call that signs with the credential it holds."""
    model = holder(rig)
    refused = RuntimeError("the store refused the request", model)
    with caplog.at_level(logging.WARNING, logger=log.name):
        log.warning("store_refused %s %r", model, model, extra={"held": model}, exc_info=refused)

    rendered: dict[str, Any] = {
        "repr": repr(model),
        "str": str(model),
        "format": f"{model}",
        "model_dump": repr(model.model_dump()),
        "model_dump json": json.dumps(model.model_dump(mode="json")),
        "model_dump_json": model.model_dump_json(),
        "log": "\n".join(DiagnosticFormatter("%(message)s").format(record) for record in caplog.records),
        "error echo": "".join(traceback.format_exception(refused)),
    }

    assert [how for how, text in rendered.items() if any(secret in text for secret in _HELD)] == []
