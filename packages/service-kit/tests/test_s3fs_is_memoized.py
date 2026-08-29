"""SK-12 — one pyarrow S3FileSystem per distinct storage_options, not one per call.

``list_lance_stems``/``exists`` fire on every dataset resolution, and each call
built a fresh ``pafs.S3FileSystem``. The filesystem is immutable and thread-safe,
so a per-option-set memo is safe and the steady state reuses one connection pool.
"""

from __future__ import annotations

from service_kit.lancekit.store import _s3fs


OPTS = {
    "endpoint": "http://localhost:9000",
    "access_key_id": "ak",
    "secret_access_key": "sk",
    "region": "us-east-1",
}


def test_equal_options_reuse_one_filesystem() -> None:
    fs1, host1 = _s3fs(OPTS)
    fs2, host2 = _s3fs(dict(OPTS))  # an equal but distinct dict must still hit the memo
    assert fs1 is fs2
    assert host1 == host2 == "localhost:9000"


def test_different_options_get_distinct_filesystems() -> None:
    fs_a, _ = _s3fs({**OPTS, "endpoint": "http://a:9000"})
    fs_b, _ = _s3fs({**OPTS, "endpoint": "http://b:9000"})
    assert fs_a is not fs_b
