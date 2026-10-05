"""Shared fixtures for the catalog member's own suite."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import settings as moto_settings
from moto.iam.access_control import IAMRequestBase
from moto.server import ThreadedMotoServer
from pydantic import BaseModel


#: The name the reference gives the second store's secret.
SECOND_REF = "second-store"


class Key(BaseModel):
    """One IAM user's access key pair on the moto server."""

    aws_access_key_id: str
    aws_secret_access_key: str


class TwoStores(BaseModel):
    """A moto server whose estate and second buckets each answer only their own key."""

    url: str
    estate_bucket: str
    second_bucket: str
    estate: Key
    second: Key

    @property
    def second_base(self) -> str:
        """The data base the second key owns, approved on the catalog with a credential reference."""
        return f"s3://{self.second_bucket}/data"

    @property
    def plain_base(self) -> str:
        """An approved data base with NO credential reference: the estate key reads it, and a vend may grant it."""
        return f"s3://{self.estate_bucket}/shared-data"


def _user_on(iam: Any, user: str, bucket: str) -> Key:  # noqa: ANN401 — a boto3 IAM client
    iam.create_user(UserName=user)
    iam.put_user_policy(
        UserName=user,
        PolicyName=f"{bucket}-only",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": ["s3:*"], "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}],
            }
        ),
    )
    key = iam.create_access_key(UserName=user)["AccessKey"]
    return Key(aws_access_key_id=key["AccessKeyId"], aws_secret_access_key=key["SecretAccessKey"])


@pytest.fixture(scope="module")
def two_stores() -> Iterator[TwoStores]:
    """Two buckets under two identities, so a read under the wrong key is REFUSED rather than served.

    moto enforces IAM once ``INITIAL_NO_AUTH_ACTION_COUNT`` is reached, which is set to 0 after the
    users exist. Its SIGNATURE check is switched off and its POLICY check is not: measured on moto 5.2.2,
    the object_store client's list request (``prefix=t%2F_versions%2F``) is rejected
    ``SignatureDoesNotMatch`` by moto's canonicalisation while boto3's identical request passes, so with
    the check on no Lance write can land at all. The access key id still decides whose policy answers,
    which is the identity the claims below are about.

    Names are per instance because moto's backends are process-global: a second server in the same
    worker (another module, or this one set up again under xdist) sees the first one's users and buckets.
    """
    suffix = uuid.uuid4().hex[:10]
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    url = f"http://{host}:{port}"
    s3 = boto3.client("s3", endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    iam = boto3.client("iam", endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    estate_bucket, second_bucket = f"estate-{suffix}", f"second-{suffix}"
    for bucket in (estate_bucket, second_bucket):
        s3.create_bucket(Bucket=bucket)
    stores = TwoStores(
        url=url,
        estate_bucket=estate_bucket,
        second_bucket=second_bucket,
        estate=_user_on(iam, f"estate-{suffix}", estate_bucket),
        second=_user_on(iam, f"second-{suffix}", second_bucket),
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(IAMRequestBase, "check_signature", lambda _self: None)
        patch.setattr(moto_settings, "INITIAL_NO_AUTH_ACTION_COUNT", 0)
        yield stores
    server.stop()


@pytest.fixture
def second_base(two_stores: TwoStores) -> str:
    """The data base ``two_store_catalog`` approves and references to the second store's key."""
    return two_stores.second_base


@pytest.fixture
def plain_base(two_stores: TwoStores) -> str:
    """The approved data base ``two_store_catalog`` reads with the estate's own key."""
    return two_stores.plain_base


@pytest.fixture
def two_store_catalog(two_stores: TwoStores, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real catalog app on the estate key, with the second store's ``data`` base approved and referenced to its key.

    The Dapr secret store is the one collaborator replaced: it answers the reference with the second
    store's bundle, in the shape ``warehouse_credentials.resolve`` reads. Vending is ``sts``, so the
    lifespan builds the deployed ``StsVendor`` with the deployed allowlist; a test that vends replaces
    only its ``_assume_role``, the call that would reach the store.
    """
    from catalog.services import warehouse_credentials

    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{two_stores.estate_bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{two_stores.estate_bucket}",
        "LANCE_S3_ENDPOINT": two_stores.url,
        "LANCE_S3_ACCESS_KEY_ID": two_stores.estate.aws_access_key_id,
        "LANCE_S3_SECRET_ACCESS_KEY": two_stores.estate.aws_secret_access_key,
        "LANCE_MULTIBASE_DATA_BASES": f"{two_stores.second_base},{two_stores.plain_base}",
        "LANCE_MULTIBASE_BASE_CREDENTIAL_REFS": f"{two_stores.second_base}={SECOND_REF}",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_VENDING_MODE": "sts",
        "LANCE_S3_STS_ENDPOINT": two_stores.url,
        "LANCE_S3_ASSUME_ROLE_ARN": "arn:aws:iam::123456789012:role/lance-vend",
        "RASK_OIDC_ENABLED": "false",
        "RASK_FGA_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)

    def _secret_store(store: str, ref: str, *, require: str) -> dict[str, str]:
        assert ref == SECOND_REF, f"the catalog asked the secret store for {ref!r}"
        return {require: two_stores.second.aws_secret_access_key, "aws_access_key_id": two_stores.second.aws_access_key_id}

    monkeypatch.setattr(warehouse_credentials, "fetch_required_secrets", _secret_store)
    warehouse_credentials.resolve.cache_clear()
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        assert client.post("/v1/namespace/mb/create", json={}).status_code in {200, 409}
        yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    warehouse_credentials.resolve.cache_clear()
