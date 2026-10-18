"""[[XC-082]] A credential rotated in the secret store is served by the running catalog once its TTL passes, with no restart.

The secret store is the sole source of every credential the catalog signs object-store requests with: its own key
pair, and each data base's credential reference ([[LH-273]]). A rotation follows the order an operator runs it in
(ADR 0166): issue a second key, write it to the store, wait out the TTL, revoke the first. moto refuses a key that is
gone (``conftest.two_stores``), so a door still signing with the revoked key fails. Each door the key travels through
is driven: the native namespace connection, a pylance write, the STS client a vend signs with, and a base read under
its own credential reference.

The store double is the sidecar's secret API, answered by respx over the real fetch. The clock is the secret cache's
own, moved past the TTL, and the catalog's credential holder is asked whether its pair is due, which is the call its
background loop makes on every wake.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterator
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
#: The secret the data base's credential reference names.
BASE_REF = "rotated-base"
#: The catalog's own secret in the store and its pair's fields (`LANCE_DAPR_SECRET_KEY` and its two field settings).
SECRET = "catalog-s3"
ID_FIELD = "access_key"
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
    """The running catalog, the store double it reads, and the identities whose keys the test issues and revokes.

    ``credential`` names which of the catalog's two store-held credentials a step means: ``estate``, its own pair in
    the ``lance`` secret, or ``base``, the data base's pair behind its credential reference.
    """

    def __init__(self, *, catalog: TestClient, iam: Any, users: dict[str, str], bundles: dict[str, dict[str, str]], clock: _Clock, second_base: str) -> None:  # noqa: ANN401 — a boto3 IAM client
        self.catalog = catalog
        self.second_base = second_base
        self.iam = iam
        self.users = users
        self.bundles = bundles
        self.clock = clock

    def issue(self, credential: str) -> Key:
        """A new key for the identity behind ``credential``, beside the one it holds."""
        return _issue(self.iam, self.users[credential])

    def revoke(self, credential: str, key: Key) -> None:
        self.iam.delete_access_key(UserName=self.users[credential], AccessKeyId=key.aws_access_key_id)

    def in_store(self, credential: str) -> Key:
        """The pair the store holds for ``credential``."""
        if credential == "estate":
            return Key(aws_access_key_id=self.bundles[SECRET][ID_FIELD], aws_secret_access_key=self.bundles[SECRET][SECRET_FIELD])
        return Key(aws_access_key_id=self.bundles[BASE_REF]["aws_access_key_id"], aws_secret_access_key=self.bundles[BASE_REF][SECRET_FIELD])

    def store(self, credential: str, key: Key) -> None:
        """Write ``key`` into the store as ``credential``, in the shape the catalog reads it."""
        if credential == "estate":
            self.bundles[SECRET] = {ID_FIELD: key.aws_access_key_id, SECRET_FIELD: key.aws_secret_access_key}
        else:
            self.bundles[BASE_REF] = {SECRET_FIELD: key.aws_secret_access_key, "aws_access_key_id": key.aws_access_key_id}

    def past_the_ttl(self) -> None:
        """Move the cache's clock past every entry's TTL, then wake the catalog's credential holder once."""
        self.clock.now += secret_cache.SECRET_TTL_SECONDS
        self.catalog.app.state.store_credential.refresh_if_due()


def _issue(iam: Any, user: str) -> Key:  # noqa: ANN401 — a boto3 IAM client
    key = iam.create_access_key(UserName=user)["AccessKey"]
    return Key(aws_access_key_id=key["AccessKeyId"], aws_secret_access_key=key["SecretAccessKey"])


def _identity_on(iam: Any, bucket: str) -> str:  # noqa: ANN401 — a boto3 IAM client
    """A new IAM user holding no key yet, allowed ``bucket`` and AssumeRole. moto, like AWS, allows two keys per user,
    which a rotation needs both of."""
    user = f"rot-{uuid.uuid4().hex[:12]}"
    iam.create_user(UserName=user)
    iam.put_user_policy(
        UserName=user,
        PolicyName="rotation",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Allow", "Action": ["s3:*"], "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]},
                    {"Effect": "Allow", "Action": ["sts:AssumeRole"], "Resource": ["*"]},
                ],
            }
        ),
    )
    return user


@pytest.fixture
def rotation(two_stores: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Rig]:  # noqa: ANN401 — conftest.TwoStores
    """The real catalog app in store mode: every key it signs with comes from the store double, none from the environment.

    Each test's identities are its own, so a revocation never touches the ``two_stores`` users, which the other
    tests on this module's moto server keep using.
    """
    from catalog.core.config import get_settings

    clock = _Clock()
    monkeypatch.setattr(secret_cache, "STORE_READS", secret_cache.SecretCache(clock=clock))
    iam = two_stores.iam()
    users = {"estate": _identity_on(iam, two_stores.estate_bucket), "base": _identity_on(iam, two_stores.second_bucket)}
    estate, base = _issue(iam, users["estate"]), _issue(iam, users["base"])
    bundles = {
        SECRET: {ID_FIELD: estate.aws_access_key_id, SECRET_FIELD: estate.aws_secret_access_key},
        BASE_REF: {SECRET_FIELD: base.aws_secret_access_key, "aws_access_key_id": base.aws_access_key_id},
    }
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{two_stores.estate_bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{two_stores.estate_bucket}",
        "LANCE_S3_ENDPOINT": two_stores.url,
        "LANCE_SECRETS_FROM_DAPR": "true",
        "LANCE_DAPR_SECRET_S3_ID_FIELD": ID_FIELD,
        "LANCE_DAPR_SECRET_S3_FIELD": SECRET_FIELD,
        "DAPR_HTTP_PORT": DAPR_PORT,
        "LANCE_MULTIBASE_DATA_BASES": two_stores.second_base,
        "LANCE_MULTIBASE_BASE_CREDENTIAL_REFS": f"{two_stores.second_base}={BASE_REF}",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_VENDING_MODE": "sts",
        "LANCE_S3_STS_ENDPOINT": two_stores.url,
        "LANCE_S3_ASSUME_ROLE_ARN": "arn:aws:iam::123456789012:role/lance-vend",
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
            yield _Rig(catalog=client, iam=iam, users=users, bundles=bundles, clock=clock, second_base=two_stores.second_base)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _a_namespace_create(rig: _Rig) -> Callable[[], httpx.Response]:
    return lambda: rig.catalog.post(f"/v1/namespace/rot{uuid.uuid4().hex[:10]}/create", json={})


def _a_table_create(rig: _Rig) -> Callable[[], httpx.Response]:
    return lambda: rig.catalog.post(f"/v1/table/rot${uuid.uuid4().hex[:10]}/create", content=_ipc(ROWS), headers=ARROW)


def _a_vend(rig: _Rig) -> Callable[[], httpx.Response]:
    table = f"rot${uuid.uuid4().hex[:10]}"
    created = rig.catalog.post(f"/v1/table/{table}/create", content=_ipc(ROWS), headers=ARROW)
    assert created.status_code == 200, created.text
    vended = rig.catalog.post(f"/management/v1/table/{table}/credentials")
    assert vended.status_code == 200, vended.text
    assert vended.json()["mode"] == "direct", f"this door must sign an STS call to prove anything: {vended.text}"
    return lambda: rig.catalog.post(f"/management/v1/table/{table}/credentials")


def _a_base_read(rig: _Rig) -> Callable[[], httpx.Response]:
    table = f"rot${uuid.uuid4().hex[:10]}"
    created = rig.catalog.post(f"/v1/table/{table}/create?data_base={rig.second_base}", content=_ipc(ROWS), headers=ARROW)
    assert created.status_code == 200, created.text
    return lambda: rig.catalog.post(
        f"/management/v1/table/{table}/changes", json={"begin_version": 0, "end_version": 1, "kind": "inserted", "columns": ["id", "v"]}
    )


@pytest.mark.parametrize(
    ("door", "credential"),
    [
        pytest.param(_a_namespace_create, "estate", id="the-native-namespace-connection"),
        pytest.param(_a_table_create, "estate", id="a-pylance-write"),
        pytest.param(_a_vend, "estate", id="the-sts-client-a-vend-signs-with"),
        pytest.param(_a_base_read, "base", id="a-base-read-under-its-own-credential-reference"),
    ],
)
def test_a_credential_rotated_in_the_store_is_served_after_its_ttl_without_a_restart(
    rotation: _Rig, door: Callable[[_Rig], Callable[[], httpx.Response]], credential: str
) -> None:
    call = door(rotation)
    assert call().status_code == 200
    old = rotation.in_store(credential)
    rotation.store(credential, rotation.issue(credential))

    rotation.past_the_ttl()
    rotation.revoke(credential, old)
    response = call()

    assert response.status_code == 200, f"the running catalog still signs with the key the store replaced: {response.text}"


def test_a_pair_the_store_stops_confirming_is_refused_once_its_ttl_passes(rotation: _Rig) -> None:
    """Fail closed: past its TTL, a pair the store did not confirm again signs nothing, and the door says it is unavailable.

    The store answers nothing here, as one whose secret was removed or that cannot be reached does. A pair served
    on regardless would keep signing with a key nobody can vouch for, so the request is refused with the spec's
    ServiceUnavailable (code 17, 503), which a client retries, rather than a 5xx that reads as a broken catalog.
    """
    call = _a_namespace_create(rotation)
    assert call().status_code == 200
    rotation.bundles.clear()

    rotation.past_the_ttl()
    response = call()

    assert (response.status_code, response.headers["content-type"]) == (503, "application/problem+json"), response.text
    assert response.json()["code"] == 17, response.text
