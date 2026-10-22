"""Shared fixtures for the catalog member's own suite."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import lance
import pytest
from fastapi.testclient import TestClient
from moto import settings as moto_settings
from moto.core.authorization import ActionAuthenticatorMixin
from moto.iam.access_control import IAMRequestBase
from moto.server import ThreadedMotoServer
from pydantic import BaseModel


#: The name the reference gives the second store's secret.
SECOND_REF = "second-store"
#: The account moto files every role and session under.
ACCOUNT = "123456789012"


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
    #: May call IAM once enforcement is on, which the users' own keys may not: issues and revokes their keys.
    admin: Key

    def iam(self) -> Any:  # noqa: ANN401 — a boto3 IAM client
        """An IAM client signed with the admin key."""
        return boto3.client(
            "iam",
            endpoint_url=self.url,
            aws_access_key_id=self.admin.aws_access_key_id,
            aws_secret_access_key=self.admin.aws_secret_access_key,
            region_name="us-east-1",
        )

    @property
    def second_base(self) -> str:
        """The data base the second key owns, approved on the catalog with a credential reference."""
        return f"s3://{self.second_bucket}/data"

    @property
    def plain_base(self) -> str:
        """An approved data base with NO credential reference: the estate key reads it, and a vend may grant it."""
        return f"s3://{self.estate_bucket}/shared-data"

    def role_on(self, bucket: str) -> str:
        """A new role allowed ``bucket``, the one a catalog's token is exchanged for; per call, so no session is another's."""
        name = f"catalog-{uuid.uuid4().hex[:12]}"
        iam = self.iam()
        iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": []}))
        iam.put_role_policy(
            RoleName=name,
            PolicyName=f"{bucket}-only",
            PolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Action": ["s3:*"], "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}],
                }
            ),
        )
        return f"arn:aws:iam::{ACCOUNT}:role/{name}"


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


def _admin(iam: Any, user: str) -> Key:  # noqa: ANN401 — a boto3 IAM client
    iam.create_user(UserName=user)
    iam.put_user_policy(
        UserName=user,
        PolicyName="iam-admin",
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["iam:*"], "Resource": ["*"]}]}),
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

    ``AssumeRoleWithWebIdentity`` is answered unauthenticated, as AWS answers it: the token in the body is the
    credential, so the call carries no signature, and moto 5.2.2's gate authenticates every action but a few Cognito
    targets (`moto/core/authorization.py`), answering the unsigned exchange 500. A session it issues is still held to
    its role's policy.
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
        admin=_admin(iam, f"admin-{suffix}"),
    )
    gate = ActionAuthenticatorMixin._authenticate_and_authorize_normal_action

    def _as_aws_gates(self: Any, resource: str = "*") -> None:  # noqa: ANN401 — the moto response class the mixin is mixed into
        if self._get_action() != "AssumeRoleWithWebIdentity":
            gate(self, resource)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(IAMRequestBase, "check_signature", lambda _self: None)
        patch.setattr(ActionAuthenticatorMixin, "_authenticate_and_authorize_normal_action", _as_aws_gates)
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
def two_store_catalog(two_stores: TwoStores, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real catalog app on a session under the estate's role, with the second store's ``data`` base approved and referenced to its key.

    The catalog's projected token is exchanged on moto's STS for a session under a role allowed the estate bucket, as
    the deployed catalog's is (ADR 0170). The Dapr secret store is the one collaborator replaced: it answers the
    reference with the second store's bundle, in the shape ``warehouse_credentials.resolve`` reads. Vending is
    ``web_identity``, so the lifespan builds the deployed ``WebIdentityVendor`` with the deployed allowlist; a test that
    vends replaces only its ``_exchange``, the call that would reach the store.
    """
    from catalog.services import warehouse_credentials

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
        "LANCE_MULTIBASE_DATA_BASES": f"{two_stores.second_base},{two_stores.plain_base}",
        "LANCE_MULTIBASE_BASE_CREDENTIAL_REFS": f"{two_stores.second_base}={SECOND_REF}",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_VENDING_MODE": "web_identity",
        "RASK_OIDC_ENABLED": "false",
        "RASK_FGA_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LANCE_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("LANCE_S3_SECRET_ACCESS_KEY", raising=False)

    def _secret_store(store: str, ref: str, *, require: str, timeout: float = 5.0, retries: int = 10, backoff: float = 3.0) -> dict[str, str]:
        assert ref == SECOND_REF, f"the catalog asked the secret store for {ref!r}"
        return {require: two_stores.second.aws_secret_access_key, "aws_access_key_id": two_stores.second.aws_access_key_id}

    monkeypatch.setattr(warehouse_credentials, "fetch_required_secrets", _secret_store)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        assert client.post("/v1/namespace/mb/create", json={}).status_code in {200, 409}
        yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


class ErasureJob:
    """A bring-your-own erasure job's three steps, pylance straight on a local table's files ([[LH-493]], ADR 0163).

    What the catalog's verification judges: on each ref, the subject's rows deleted by row id ([[LH-281]]: the history
    then records ids, never the identifier), every fragment that held them compacted with deletions materialised, and
    every user index dropped, since no segment the job commits is taken as proof; the job rebuilds its indexes once the
    obligation is released. The commit path a deployed job takes, through ``version/create``, is the stock-client
    suite's claim; this one is the verification's.
    """

    def __init__(self, uri: str, predicate: str) -> None:
        self.uri = uri
        self.predicate = predicate

    def head(self, ref: str | None = None) -> Any:  # noqa: ANN401 — a lance.LanceDataset
        dataset = lance.dataset(self.uri)
        return dataset if ref is None else dataset.checkout_version((ref, None))

    def delete(self, ref: str | None = None) -> None:
        head = self.head(ref)
        ids = head.to_table(columns=[], filter=self.predicate, with_row_id=True).column("_rowid").to_pylist()
        if ids:
            head.delete(f"_rowid IN ({', '.join(str(row_id) for row_id in ids)})")

    def compact(self, ref: str | None = None) -> None:
        self.head(ref).optimize.compact_files(materialize_deletions_threshold=0.0)

    def drop_indices(self, ref: str | None = None) -> None:
        for name in self._user_indices(ref):
            self.head(ref).drop_index(name)

    def rebuild_indices(self, ref: str | None = None) -> None:
        """What the job does after the release, and what no verification takes as proof before it."""
        head = self.head(ref)
        for index in head.describe_indices():
            if not str(index.name).startswith("__"):
                (column,) = index.field_names
                head.create_scalar_index(column, index_type=str(index.index_type).upper(), name=index.name, replace=True)

    def run(self) -> None:
        """Every step on every ref: each branch, then main."""
        for ref in [*lance.dataset(self.uri).branches.list(), None]:
            self.delete(ref)
            self.compact(ref)
            self.drop_indices(ref)

    def _user_indices(self, ref: str | None) -> list[str]:
        return sorted(str(index.name) for index in self.head(ref).describe_indices() if not str(index.name).startswith("__"))


@pytest.fixture
def erasure_job() -> type[ErasureJob]:
    """The job's stand-in, constructed per table: ``erasure_job(uri, predicate).run()``."""
    return ErasureJob


class GovernedCatalog:
    """The catalog app as deployed with authorization on: a loopback Dex verifies the caller and OpenFGA decides ([[LH-235]]).

    Nothing stands in for the governance plane: the lifespan builds the deployed verifier against ``dex_issuer`` and the
    deployed OpenFGA client against the test's own store, and every tuple a test relies on is written by the door that
    writes it in production, or seeded straight to the store as the chart's bootstrap hook seeds it.
    """

    #: The Dex connector the lane's people sign in through, and the idp id the deployment maps it to.
    CONNECTOR, IDP = "local", "dex"

    def __init__(self, client: TestClient, store: Any, dex_issuer: Any) -> None:  # noqa: ANN401 — root conftest's OpenFgaStore and DexIssuer
        self.client = client
        self.store = store
        self._dex = dex_issuer

    def person(self, user_id: str) -> str:
        """The OpenFGA user the person ``user_id`` is, the one a tuple naming them carries."""
        return f"user:{self.IDP}~{user_id}"

    def bearer(self, user_id: str) -> dict[str, str]:
        """The headers of a request the person ``user_id`` signs in for."""
        return {"Authorization": f"Bearer {self._dex.mint(user_id, connector=self.CONNECTOR, audience='lance-catalog')}"}


@pytest.fixture
def governed_catalog(openfga_store: Any, dex_issuer: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[GovernedCatalog]:  # noqa: ANN401 — root conftest's fixtures
    """The catalog on a real ``dir`` namespace, signed-in callers only, each decision OpenFGA's on a store of this test's own."""
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": dex_issuer.issuer,
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_OIDC_ALLOW_INSECURE": "true",
        "RASK_OIDC_CONNECTORS": json.dumps({GovernedCatalog.CONNECTOR: GovernedCatalog.IDP}),
        "RASK_FGA_ENABLED": "true",
        "RASK_FGA_API_URL": openfga_store.url,
        "RASK_FGA_STORE_ID": openfga_store.store_id,
        "RASK_FGA_MODEL_ID": openfga_store.model_id,
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        yield GovernedCatalog(client, openfga_store, dex_issuer)
    app.dependency_overrides.clear()
    get_settings.cache_clear()
