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
