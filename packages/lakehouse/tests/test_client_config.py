"""S3 client construction: retry config (PS-01), per-client region, the credential it signs with, the endpoint env chain."""

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr

from lakehouse.storage import client as client_module
from lakehouse.storage.credential import S3KeyPair


def test_s3_client_retries_transient_errors_to_the_configured_attempts(monkeypatch):
    """Behavioural pin for PS-01: the retry policy must actually fire, not merely sit in Config.

    The textual pin above cannot catch the wrongPrescription PS-01 warned about — an
    unregister that PASSES the handler (or its `retry-config-s3` unique_id) really does
    strip botocore's retry handler, silently disabling the adaptive retries the Config
    block asks for. So this observes the behaviour: a transient 500 is retried to the
    configured 4 total attempts; a stripped handler drops that to 1 and fails here.
    """
    import pytest
    from botocore.awsrequest import AWSResponse
    from botocore.compat import HTTPHeaders
    from botocore.exceptions import ClientError

    from lakehouse.storage.client import s3_client

    # Retry backoff sleeps between attempts (botocore.endpoint); the delay is not under test.
    monkeypatch.setattr("time.sleep", lambda _s: None)

    client = s3_client("http://s3.invalid", key_pair=_PASSED)
    attempts = 0

    class _EmptyBody:
        def stream(self):
            yield b""

    def respond_500(request, **_kwargs):
        nonlocal attempts
        attempts += 1
        return AWSResponse(request.url, 500, HTTPHeaders(), _EmptyBody())

    # Short-circuits the wire: botocore takes a non-None before-send response as THE
    # http response, so the retry loop runs for real with no network.
    client.meta.events.register("before-send.s3.HeadBucket", respond_500)
    with pytest.raises(ClientError):
        client.head_bucket(Bucket="smoke")

    assert attempts == 4  # 1 + the 3 retries Config(retries={"max_attempts": 3}) configures


def test_s3_client_region_overrides_the_env_for_this_client_only(monkeypatch):
    """A per-store region must reach the client, or every backend is pinned to `AWS_REGION`.

    The callers holding a region of their own (`lakehouse.storage.conditional_records`, whose
    `storage_options` carry one per warehouse) had no way to pass it through this seam, which is
    exactly what kept them on a hand-rolled `boto3.client`. Omitted still resolves from the env.
    """
    from lakehouse.storage.client import s3_client

    monkeypatch.setenv("AWS_REGION", "us-east-1")

    assert s3_client(key_pair=_PASSED, region="eu-north-1").meta.region_name == "eu-north-1"
    assert s3_client(key_pair=_PASSED).meta.region_name == "us-east-1"


#: A pair each ambient source names, so a request signed with any of them is told apart from the one the caller passed.
_AMBIENT = "AKIDAMBIENT"
_PASSED = S3KeyPair(access_key_id="AKIDPASSED", secret_access_key=SecretStr("passed-secret"), session_token=SecretStr("passed-token"))
_KEYS = f"aws_access_key_id = {_AMBIENT}\naws_secret_access_key = ambient-secret\n"


def _ambient_sources(tmp: Path) -> dict[str, dict[str, str]]:
    """One environment per source botocore's default chain reads (`botocore.credentials.create_credential_resolver` on
    botocore 1.43.58: env, assume-role, assume-role-with-web-identity, sso, shared-credentials-file, login, custom-process,
    config-file, ec2-credentials-file, boto-config, container-role, iam-role), and per variable that picks one. Every
    remote source is aimed at a closed local port, so a client that consults one fails at once instead of dialling out."""
    files = {
        "credentials": f"[default]\n{_KEYS}",
        "profiles": f"[profile picked]\n{_KEYS}",
        "config": f"[default]\n{_KEYS}",
        "process": f"[default]\ncredential_process = {sys.executable} {tmp / 'process.py'}\n",
        "process.py": f'print(\'{{"Version": 1, "AccessKeyId": "{_AMBIENT}", "SecretAccessKey": "s"}}\')\n',
        "assume": f"[default]\nrole_arn = arn:aws:iam::000000000000:role/r\nsource_profile = base\n[profile base]\n{_KEYS}",
        "sso": "[default]\nsso_start_url = http://127.0.0.1:9/start\nsso_region = us-east-1\nsso_account_id = 000000000000\nsso_role_name = r\n",
        "login": "[default]\nlogin_session = arn:aws:iam::000000000000:user/u\n",
        "legacy": f"AWSAccessKeyId={_AMBIENT}\nAWSSecretKey=ambient-secret\n",
        "boto": f"[Credentials]\n{_KEYS}",
        "token": "a-web-identity-token",
    }
    for name, text in files.items():
        (tmp / name).write_text(text)
    closed = "http://127.0.0.1:9"
    return {
        "AWS_ACCESS_KEY_ID": {"AWS_ACCESS_KEY_ID": _AMBIENT, "AWS_SECRET_ACCESS_KEY": "ambient-secret"},
        "AWS_SESSION_TOKEN": {"AWS_ACCESS_KEY_ID": _AMBIENT, "AWS_SECRET_ACCESS_KEY": "ambient-secret", "AWS_SESSION_TOKEN": "ambient-token"},
        "AWS_SECURITY_TOKEN": {"AWS_ACCESS_KEY_ID": _AMBIENT, "AWS_SECRET_ACCESS_KEY": "ambient-secret", "AWS_SECURITY_TOKEN": "ambient-token"},
        "AWS_CREDENTIAL_EXPIRATION": {"AWS_ACCESS_KEY_ID": _AMBIENT, "AWS_SECRET_ACCESS_KEY": "s", "AWS_CREDENTIAL_EXPIRATION": "2999-01-01T00:00:00Z"},
        "AWS_SHARED_CREDENTIALS_FILE": {"AWS_SHARED_CREDENTIALS_FILE": str(tmp / "credentials")},
        "AWS_PROFILE": {"AWS_CONFIG_FILE": str(tmp / "profiles"), "AWS_PROFILE": "picked"},
        "AWS_DEFAULT_PROFILE": {"AWS_CONFIG_FILE": str(tmp / "profiles"), "AWS_DEFAULT_PROFILE": "picked"},
        "AWS_PROFILE-absent": {"AWS_PROFILE": "absent"},
        "AWS_CONFIG_FILE": {"AWS_CONFIG_FILE": str(tmp / "config")},
        "credential_process": {"AWS_CONFIG_FILE": str(tmp / "process")},
        "role_arn-source_profile": {"AWS_CONFIG_FILE": str(tmp / "assume"), "AWS_ENDPOINT_URL_STS": closed},
        "sso": {"AWS_CONFIG_FILE": str(tmp / "sso")},
        "login_session": {"AWS_CONFIG_FILE": str(tmp / "login")},
        "AWS_WEB_IDENTITY_TOKEN_FILE": {
            "AWS_WEB_IDENTITY_TOKEN_FILE": str(tmp / "token"),
            "AWS_ROLE_ARN": "arn:aws:iam::000000000000:role/r",
            "AWS_ROLE_SESSION_NAME": "ambient",
            "AWS_ENDPOINT_URL_STS": closed,
        },
        "AWS_CREDENTIAL_FILE": {"AWS_CREDENTIAL_FILE": str(tmp / "legacy")},
        "BOTO_CONFIG": {"BOTO_CONFIG": str(tmp / "boto")},
        "AWS_CONTAINER_CREDENTIALS_FULL_URI": {"AWS_CONTAINER_CREDENTIALS_FULL_URI": f"{closed}/creds", "AWS_CONTAINER_AUTHORIZATION_TOKEN": "ambient"},
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": {
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/creds",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": str(tmp / "token"),
        },
        "AWS_EC2_METADATA_SERVICE_ENDPOINT": {"AWS_EC2_METADATA_SERVICE_ENDPOINT": closed, "AWS_METADATA_SERVICE_NUM_ATTEMPTS": "1"},
    }


_SOURCES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CREDENTIAL_EXPIRATION",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_PROFILE-absent",
    "AWS_CONFIG_FILE",
    "credential_process",
    "role_arn-source_profile",
    "sso",
    "login_session",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CREDENTIAL_FILE",
    "BOTO_CONFIG",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_EC2_METADATA_SERVICE_ENDPOINT",
)


@pytest.mark.parametrize("endpoint", [pytest.param("http://store.invalid:9000", id="named-store"), pytest.param("", id="aws-proper")])
@pytest.mark.parametrize("source", _SOURCES)
def test_a_store_client_signs_with_the_pair_it_is_given_and_no_other(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str, endpoint: str) -> None:
    """Every boto3 client the lakehouse builds signs with the key pair it was handed, whatever the environment holds,
    and one handed none is refused: botocore's default chain would sign with whatever it finds, which in a pod is a
    credential nobody scoped ([[XC-219]])."""
    from botocore.awsrequest import AWSResponse
    from botocore.compat import HTTPHeaders

    from lakehouse.storage.errors import MissingStoreCredentialError
    from lakehouse.storage.options import StorageOptions, object_client

    for name in [name for name in os.environ if name.startswith(("AWS_", "BOTO_"))]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    sources = _ambient_sources(tmp_path)
    assert sources.keys() == set(_SOURCES)
    for name, value in sources[source].items():
        monkeypatch.setenv(name, value)
    # The session is built once per process: a fresh one here reads this environment, so the case proves the pin.
    client_module.aws_session.cache_clear()
    options = {"aws_endpoint": endpoint} if endpoint else {}
    signed: list[dict[str, str]] = []

    class _EmptyBody:
        def stream(self, **_kwargs: object) -> Iterator[bytes]:
            yield b""

    def answer(request: Any, **_kwargs: object) -> AWSResponse:
        signed.append({name: value.decode() if isinstance(value, bytes) else value for name, value in request.headers.items()})
        return AWSResponse(request.url, 200, HTTPHeaders(), _EmptyBody())

    client = object_client(StorageOptions(key_pair=_PASSED, options=options))
    client.meta.events.register("before-send.s3.HeadBucket", answer)
    client.head_bucket(Bucket="bucket")

    (headers,) = signed
    assert f"Credential={_PASSED.access_key_id}/" in headers["Authorization"]
    assert headers["X-Amz-Security-Token"] == "passed-token"
    with pytest.raises(MissingStoreCredentialError, match="key pair"):
        object_client(StorageOptions(options=options))


@pytest.mark.parametrize(
    ("value", "expected"),
    [pytest.param("http://rask", "http://rask", id="set"), pytest.param("", None, id="empty-is-unset")],
)
def test_configured_endpoint_reads_its_one_name_and_takes_empty_as_unset(monkeypatch: pytest.MonkeyPatch, value: str, expected: str | None) -> None:
    from lakehouse.storage.client import configured_endpoint

    monkeypatch.setenv("RASK_S3_ENDPOINT_URL", value)

    assert configured_endpoint() == expected


def test_a_store_client_is_built_from_a_key_pair_holding_both_halves_and_from_nothing_else() -> None:
    """The seam's own refusals: a credential of another shape, and a pair naming no key id while it carries a token,
    which botocore would otherwise complete from its default chain."""
    from lakehouse.storage.client import s3_client
    from lakehouse.storage.errors import MissingStoreCredentialError

    with pytest.raises(TypeError, match="S3KeyPair"):
        s3_client("http://store.invalid:9000", key_pair=cast("S3KeyPair", ("AKIA", "secret")))
    lone_token = S3KeyPair(access_key_id="", secret_access_key=SecretStr("secret"), session_token=SecretStr("token"))
    with pytest.raises(MissingStoreCredentialError, match="access_key_id"):
        s3_client("http://store.invalid:9000", key_pair=lone_token)
