"""[[XC-082]] A base's credential rotated in the secret store is served by the running catalog once its TTL passes, with no restart.

The secret store is the sole source of each data base's credential reference ([[LH-273]]); the catalog's own identity
is its ServiceAccount token (ADR 0170), whose sessions renew themselves
(`test_the_catalog_signs_only_with_sessions_its_token_is_exchanged_for.py`). A rotation follows the order an operator
runs it in (ADR 0166): issue a second key, write it to the store, wait out the TTL, revoke the first. moto refuses a
key that is gone (``conftest.two_stores``), so a base read still signing with the revoked key fails.

The store double is the sidecar's secret API, answered by respx over the real fetch. The clock is the secret cache's
own, moved past the TTL.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import BaseModel

from service_kit.governed import secret_cache


#: The port the store double answers on. Nothing listens there; respx answers the sidecar's secret API.
DAPR_PORT = "3597"
SIDECAR = f"http://localhost:{DAPR_PORT}/v1.0/secrets/lance-secrets"
#: The secret the data base's credential reference names, and the field holding its secret key (`LANCE_DAPR_SECRET_S3_FIELD`).
BASE_REF = "rotated-base"
SECRET_FIELD = "secret_key"
ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
ROWS = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": ["a", "b", "c"]})


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


class Key(BaseModel):
    """One access key pair, as moto issued it."""

    aws_access_key_id: str
    aws_secret_access_key: str


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Rig:
    """The running catalog, the store double it reads, and the base's identity whose keys the test issues and revokes."""

    def __init__(self, *, catalog: TestClient, iam: Any, user: str, bundles: dict[str, dict[str, str]], clock: _Clock, second_base: str) -> None:  # noqa: ANN401 — a boto3 IAM client
        self.catalog = catalog
        self.second_base = second_base
        self.iam = iam
        self.user = user
        self.bundles = bundles
        self.clock = clock

    def issue(self) -> Key:
        """A new key for the base's identity, beside the one it holds."""
        return _issue(self.iam, self.user)

    def revoke(self, key: Key) -> None:
        self.iam.delete_access_key(UserName=self.user, AccessKeyId=key.aws_access_key_id)

    def in_store(self) -> Key:
        """The pair the store holds for the base."""
        return Key(aws_access_key_id=self.bundles[BASE_REF]["aws_access_key_id"], aws_secret_access_key=self.bundles[BASE_REF][SECRET_FIELD])

    def store(self, key: Key) -> None:
        """Write ``key`` into the store as the base's credential, in the shape the catalog reads it."""
        self.bundles[BASE_REF] = {SECRET_FIELD: key.aws_secret_access_key, "aws_access_key_id": key.aws_access_key_id}

    def past_the_ttl(self) -> None:
        """Move the cache's clock past every entry's TTL."""
        self.clock.now += secret_cache.SECRET_TTL_SECONDS


def _issue(iam: Any, user: str) -> Key:  # noqa: ANN401 — a boto3 IAM client
    key = iam.create_access_key(UserName=user)["AccessKey"]
    return Key(aws_access_key_id=key["AccessKeyId"], aws_secret_access_key=key["SecretAccessKey"])


def _identity_on(iam: Any, bucket: str) -> str:  # noqa: ANN401 — a boto3 IAM client
    """A new IAM user holding no key yet, allowed ``bucket``. moto, like AWS, allows two keys per user, which a rotation
    needs both of."""
    user = f"rot-{uuid.uuid4().hex[:12]}"
    iam.create_user(UserName=user)
    iam.put_user_policy(
        UserName=user,
        PolicyName="rotation",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": ["s3:*"], "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}],
            }
        ),
    )
    return user


@pytest.fixture
def rotation(two_stores: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Rig]:  # noqa: ANN401 — conftest.TwoStores
    """The real catalog app in store mode: its own IO on its token's session, the base's key from the store double.

    The base's identity is this test's own, so a revocation never touches the ``two_stores`` users, which the other
    tests on this module's moto server keep using.
    """
    from catalog.core.config import get_settings

    clock = _Clock()
    monkeypatch.setattr(secret_cache, "STORE_READS", secret_cache.SecretCache(clock=clock))
    iam = two_stores.iam()
    user = _identity_on(iam, two_stores.second_bucket)
    base = _issue(iam, user)
    bundles = {BASE_REF: {SECRET_FIELD: base.aws_secret_access_key, "aws_access_key_id": base.aws_access_key_id}}
    token = tmp_path / "projected-rask-object-store-token"
    token.write_text("projected-for-rask-object-store")
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{two_stores.estate_bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{two_stores.estate_bucket}",
        "LANCE_S3_ENDPOINT": two_stores.url,
        "LANCE_S3_WEB_IDENTITY_TOKEN_FILE": str(token),
        "LANCE_S3_STS_ENDPOINT": two_stores.url,
        "LANCE_S3_ASSUME_ROLE_ARN": two_stores.role_on(two_stores.estate_bucket),
        "LANCE_SECRETS_FROM_DAPR": "true",
        "LANCE_DAPR_SECRET_S3_FIELD": SECRET_FIELD,
        "DAPR_HTTP_PORT": DAPR_PORT,
        "LANCE_MULTIBASE_DATA_BASES": two_stores.second_base,
        "LANCE_MULTIBASE_BASE_CREDENTIAL_REFS": f"{two_stores.second_base}={BASE_REF}",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "RASK_OIDC_ENABLED": "false",
        "RASK_FGA_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LANCE_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("LANCE_S3_SECRET_ACCESS_KEY", raising=False)

    def _sidecar(_request: httpx.Request, name: str) -> httpx.Response:
        return httpx.Response(200, json=bundles[name]) if name in bundles else httpx.Response(404)

    get_settings.cache_clear()
    from catalog.main import app

    with respx.mock(assert_all_called=False) as sidecar:
        sidecar.get(url__regex=rf"^{re.escape(SIDECAR)}/(?P<name>[^/?]+)$").mock(side_effect=_sidecar)
        with TestClient(app) as client:
            assert client.post("/v1/namespace/rot/create", json={}).status_code in {200, 409}
            yield _Rig(catalog=client, iam=iam, user=user, bundles=bundles, clock=clock, second_base=two_stores.second_base)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _a_base_read(rig: _Rig) -> Callable[[], httpx.Response]:
    table = f"rot${uuid.uuid4().hex[:10]}"
    created = rig.catalog.post(f"/v1/table/{table}/create?data_base={rig.second_base}", content=_ipc(ROWS), headers=ARROW)
    assert created.status_code == 200, created.text
    return lambda: rig.catalog.post(
        f"/management/v1/table/{table}/changes", json={"begin_version": 0, "end_version": 1, "kind": "inserted", "columns": ["id", "v"]}
    )


def test_a_bases_credential_rotated_in_the_store_is_served_after_its_ttl_without_a_restart(rotation: _Rig) -> None:
    call = _a_base_read(rotation)
    assert call().status_code == 200
    old = rotation.in_store()
    rotation.store(rotation.issue())

    rotation.past_the_ttl()
    rotation.revoke(old)
    response = call()

    assert response.status_code == 200, f"the running catalog still reads the base with the key the store replaced: {response.text}"
