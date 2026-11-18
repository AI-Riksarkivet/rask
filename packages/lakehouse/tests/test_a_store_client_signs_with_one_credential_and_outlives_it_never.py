"""The object client the storage seam keeps signs with one credential, holds no secret in its key, and is let go when its
credential is replaced or expires (``lakehouse.storage_options.object_client``, zero trust: no credential is kept past
its use).

The process keeps one botocore client per credential, shared by every control-root record, claim, seal and fence, since a
client built per call cost a fresh connection and some 6 ms of the interpreter lock each (ADR 0240 §10). Kept, a
client holds its credential's secret in memory: before, the cache's key held the secret and session token in clear, and
a rotated credential's client stayed until 32 newer ones evicted it. The root conftest empties the cache around every
test, so nothing else exercises the cache as a process runs it; this does, through the real seam (boto3 builds a client
without reaching any store).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from lakehouse import storage_options
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.storage_options import StorageOptions, object_client
from lakehouse.store_session import SessionCache, WebIdentity


def _options(access_key: str, secret: str, token: str, **options: str) -> StorageOptions:
    pair = S3KeyPair(access_key_id=access_key, secret_access_key=SecretStr(secret), session_token=SecretStr(token))
    return StorageOptions(key_pair=pair, options={"aws_endpoint": "http://store.invalid:9000", **options})


def _signs_with(client: object) -> tuple[str, str, str]:
    """The credential botocore's signer for ``client`` signs with."""
    credentials: Any = getattr(getattr(client, "_request_signer"), "_credentials")  # noqa: B009 — botocore's private signer, what a request is signed with
    return credentials.access_key, credentials.secret_key, credentials.token


def test_each_credential_signs_with_its_own_client_and_a_replaced_or_expired_one_keeps_none() -> None:
    first, second = _options("AK1", "secret-one", "token-one"), _options("AK2", "secret-two", "token-two")

    assert object_client(first) is object_client(first), "one credential shares one client"
    assert (_signs_with(object_client(first)), _signs_with(object_client(second))) == (
        ("AK1", "secret-one", "token-one"),
        ("AK2", "secret-two", "token-two"),
    )
    assert not any(secret in repr(key) for key in storage_options._clients for secret in ("secret-one", "token-one", "secret-two", "token-two"))

    # The store session renews: the session it replaces signs nothing more, and its client is gone.
    sessions = SessionCache()
    identity = WebIdentity(token_file=Path("/var/run/token"), role_arn="arn:aws:iam::0:role/catalog", region="us-east-1", endpoint=None)
    assert first.key_pair is not None and second.key_pair is not None
    sessions.hold(identity, first.key_pair, asked_at=0.0, lifetime_seconds=900)
    sessions.hold(identity, second.key_pair, asked_at=450.0, lifetime_seconds=900)
    assert [key[3] for key in storage_options._clients] == ["AK2"], "the replaced session's client is let go"

    # A vended credential past its expiry (the spec's `expires_at_millis`) is let go at the next call, whoever makes it.
    vended = _options("AK3", "secret-three", "token-three", expires_at_millis=str(int(time.time() * 1000) + 60_000))
    expired = _options("AK4", "secret-four", "token-four", expires_at_millis=str(int(time.time() * 1000) - 1))
    object_client(vended)
    object_client(expired)
    object_client(second)
    assert sorted(str(key[3]) for key in storage_options._clients) == ["AK2", "AK3"], "an expired credential's client is let go"
