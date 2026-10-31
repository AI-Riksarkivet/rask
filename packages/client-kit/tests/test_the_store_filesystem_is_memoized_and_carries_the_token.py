"""SK-12 — one pyarrow S3FileSystem per distinct storage_options, not one per call, carrying the whole credential.

``list_lance_stems``/``exists`` fire on every dataset resolution, and each call
built a fresh ``pafs.S3FileSystem``. The filesystem is immutable and thread-safe,
so a per-option-set memo is safe and the steady state reuses one connection pool.
"""

from __future__ import annotations

from pydantic import SecretStr

from client_kit.lancekit.store import _s3fs
from service_kit.governed.store_credential import S3KeyPair
from service_kit.lakehouse.objectfs import lance_storage_options


OPTS = {
    "endpoint": "http://localhost:9000",
    "access_key_id": "ak",
    "secret_access_key": "sk",
    "region": "us-east-1",
}


def test_different_options_get_distinct_filesystems() -> None:
    fs_a, _ = _s3fs({**OPTS, "endpoint": "http://a:9000"})
    fs_b, _ = _s3fs({**OPTS, "endpoint": "http://b:9000"})
    assert fs_a is not fs_b


def test_the_store_filesystem_carries_the_session_token() -> None:
    """A vended credential is a triple, and the token is the half that carries its scope.

    pyarrow falls back to the default chain for whatever it was not given, so a filesystem built without the token
    signs with the pod's own role, broader than the credential the catalog scoped: the fail-open `s3_filesystem`'s
    docstring warns about.
    """
    options = lance_storage_options(
        "http://127.0.0.1:1", S3KeyPair(access_key_id="AKIA", secret_access_key=SecretStr("secret"), session_token=SecretStr("TOKEN")), "us-east-1"
    )
    filesystem, _host = _s3fs(options.in_clear())
    assert "TOKEN" in str(filesystem.__reduce__()), "the session token was dropped at the store seam"
