"""S3 client construction: retry config (PS-01) and the pure HCP bridge (PS-05)."""

import base64
import hashlib
import os
from pathlib import Path


_CLIENT_SRC = Path(__file__).resolve().parents[1] / "src" / "storage" / "client.py"


def test_s3_client_keeps_configured_retries():
    """The adaptive retry policy in Config must reach the built client."""
    from storage import s3_client

    client = s3_client()
    retries = client.meta.config.retries
    # botocore normalises max_attempts=3 → total_max_attempts=4 and keeps the mode.
    assert retries["mode"] == "adaptive"
    assert retries["total_max_attempts"] == 4


def test_client_does_not_strip_its_own_retry_handler():
    """No `unregister("needs-retry.s3")`: it matched no handler (removed nothing) yet
    read as though it disabled the very retries the Config block asks for."""
    src = _CLIENT_SRC.read_text(encoding="utf-8")
    assert 'unregister("needs-retry.s3")' not in src


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

    from storage import s3_client

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    # Retry backoff sleeps between attempts (botocore.endpoint); the delay is not under test.
    monkeypatch.setattr("time.sleep", lambda _s: None)

    client = s3_client("http://s3.invalid")
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


def test_derive_hcp_creds_returns_creds_without_mutating_environ(monkeypatch):
    from storage import derive_hcp_creds

    monkeypatch.setenv("HCP_USERNAME", "alice")
    monkeypatch.setenv("HCP_PASSWORD", "secret")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    creds = derive_hcp_creds()

    # Values derived from the fake inputs above (base64 of the username, md5 of the
    # password) — not real credentials.
    assert creds == {
        "access_key": base64.b64encode(b"alice").decode(),
        "secret_key": hashlib.md5(b"secret").hexdigest(),  # noqa: S324
    }
    # The defect this closes: the derivation must not touch the process-global env,
    # so one process can address more than one backend.
    assert "AWS_ACCESS_KEY_ID" not in os.environ
    assert "AWS_SECRET_ACCESS_KEY" not in os.environ


def test_derive_hcp_creds_defers_to_existing_aws_keys(monkeypatch):
    from storage import derive_hcp_creds

    monkeypatch.setenv("HCP_USERNAME", "alice")
    monkeypatch.setenv("HCP_PASSWORD", "secret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIADIRECT")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    assert derive_hcp_creds() is None


def test_derive_hcp_creds_noop_without_hcp(monkeypatch):
    from storage import derive_hcp_creds

    monkeypatch.delenv("HCP_USERNAME", raising=False)
    monkeypatch.delenv("HCP_PASSWORD", raising=False)

    assert derive_hcp_creds() is None


def test_s3_client_applies_hcp_creds_without_env_mutation(monkeypatch):
    """The HCP bridge reaches the client per-call, so no caller has to pre-mutate env."""
    from storage import s3_client

    monkeypatch.setenv("HCP_USERNAME", "alice")
    monkeypatch.setenv("HCP_PASSWORD", "secret")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    client = s3_client()

    assert client._request_signer._credentials.access_key == base64.b64encode(b"alice").decode()
    assert "AWS_ACCESS_KEY_ID" not in os.environ
    assert "AWS_SECRET_ACCESS_KEY" not in os.environ


def test_s3_client_region_overrides_the_env_for_this_client_only(monkeypatch):
    """A per-store region must reach the client, or every backend is pinned to `AWS_REGION`.

    The callers holding a region of their own (`service_kit.lakehouse.records`, whose
    `storage_options` carry one per warehouse) had no way to pass it through this seam, which is
    exactly what kept them on a hand-rolled `boto3.client`. Omitted still resolves from the env.
    """
    from storage import s3_client

    monkeypatch.setenv("AWS_REGION", "us-east-1")

    assert s3_client(region="eu-north-1").meta.region_name == "eu-north-1"
    assert s3_client().meta.region_name == "us-east-1"


def test_s3_client_forwards_a_session_token():
    """Vended temporary credentials are a TRIPLE, and dropping the token is not a visible failure:
    SigV4 signs happily with the key pair alone and the store answers 403 `InvalidAccessKeyId`, which
    reads as a credential problem rather than as a client that discarded a field it was handed."""
    from storage import s3_client

    client = s3_client("http://rf:9000", access_key="ak", secret_key="sk", session_token="tok")

    creds = client._request_signer._credentials
    assert (creds.access_key, creds.secret_key, creds.token) == ("ak", "sk", "tok")


def test_s3_client_refuses_a_session_token_without_its_key_pair():
    """botocore IGNORES `aws_session_token` when the key pair is absent and falls back to the env
    credential chain, so a mis-wired caller would sign as the wrong identity with no error anywhere.
    Refusing at the seam turns that into an exception at the one place that can still name it."""
    import pytest

    from storage import s3_client

    with pytest.raises(ValueError, match="session_token"):
        s3_client("http://rf:9000", session_token="tok")
