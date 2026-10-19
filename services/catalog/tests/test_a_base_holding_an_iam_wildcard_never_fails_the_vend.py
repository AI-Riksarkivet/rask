"""[[LH-437]] A table's base holding an IAM wildcard is vended around, and never fails the vend.

A table's bases come off its manifest, which its writer chose, and the register door admits a base inside the
table's own root, or one directory beneath an approved data base, on its location alone. ``*`` is a wildcard in the
session policy a credential is scoped by, with no escape, so no statement can name such a base without widening it.
A base inside the table's own root needs none: the table's own statements already reach it, so the vend is direct
and the policy says nothing of the base. A directory beneath the approved base is reachable only with the catalog's
own credential, so both vending doors answer as they do for any base the policy cannot address: server-mediated.

Driven through the real register, credentials and describe doors on the two-store estate
(``conftest.two_store_catalog``), whose lifespan builds the deployed ``WebIdentityVendor``; only its token exchange
is replaced, keeping the policy it would send.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from service_kit.governed.store_credential import S3KeyPair
from service_kit.governed.store_session import IssuedSession


def _options(two_stores: Any) -> dict[str, str]:  # noqa: ANN401 — conftest.TwoStores
    return {"endpoint": two_stores.url, "allow_http": "true", "region": "us-east-1", **two_stores.estate.model_dump()}


def _capture_policy(client: TestClient) -> dict[str, Any]:
    sent: dict[str, Any] = {}

    def _exchange(_identity: Any, *, session_name: str, duration_seconds: int, policy: str | None = None) -> IssuedSession:  # noqa: ANN401
        sent["policy"] = json.loads(str(policy))
        return IssuedSession(
            key_pair=S3KeyPair(access_key_id="AK", secret_access_key=SecretStr("SK"), session_token=SecretStr("ST")),
            expiration=datetime(2030, 1, 1, tzinfo=UTC),
        )

    client.app.state.vendor._exchange = _exchange  # noqa: SLF001 — the one call that would reach the store
    return sent


@pytest.mark.parametrize(
    "door", [("/management/v1/table/{table}/credentials", {}), ("/v1/table/{table}/describe", {"vend_credentials": "true"})], ids=["credentials", "describe"]
)
@pytest.mark.parametrize(("where", "direct"), [("own-root", True), ("data-base", False)])
def test_a_base_holding_a_wildcard_never_fails_the_vend(
    two_store_catalog: TestClient,
    two_stores: Any,  # noqa: ANN401 — conftest.TwoStores
    plain_base: str,
    door: tuple[str, dict[str, str]],
    where: str,
    direct: bool,
) -> None:
    location = f"wild-{uuid.uuid4().hex[:8]}"
    root = f"s3://{two_stores.estate_bucket}/{location}"
    base = f"{root}/sub*x" if where == "own-root" else f"{plain_base}/{location}*x"
    lance.write_dataset(
        pa.table({"id": pa.array([1], pa.int64())}),
        root,
        storage_options=_options(two_stores),
        data_storage_version="2.2",
        enable_stable_row_ids=True,
        initial_bases=[lance.DatasetBasePath(base, is_dataset_root=False, name="wild")],
    )
    table = f"mb$wild{uuid.uuid4().hex[:8]}"
    registered = two_store_catalog.post(f"/v1/table/{table}/register", json={"location": location})
    assert registered.status_code == 200, f"precondition: the register door admits the base on its location: {registered.text}"
    sent = _capture_policy(two_store_catalog)
    path, params = door

    answered = TestClient(two_store_catalog.app, raise_server_exceptions=False).post(path.format(table=table), params=params)

    assert answered.status_code == 200, answered.text
    assert ("policy" in sent) is direct, f"vended {'server-mediated' if direct else 'direct'}: {answered.text}"
    assert "*x" not in json.dumps(sent.get("policy", {})), f"the policy names the base's wildcard: {sent['policy']}"
