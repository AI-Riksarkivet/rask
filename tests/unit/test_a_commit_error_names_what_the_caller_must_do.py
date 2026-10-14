"""The commit-error taxonomy the catalog's own commits share, and the vending mode's fail-closed STS guard.

A conflict is classified by what the caller must do next: a stale commit after an Overwrite is a non-retryable 400,
a lost race a 409, a schema mismatch a 400, a store outage a 503, never a blanket 409 that loops a doomed retry.
"""

from __future__ import annotations

import pytest
from lance_namespace import ConcurrentModificationError, InvalidInputError, ServiceUnavailableError


def test_classify_commit_error_maps_the_taxonomy() -> None:
    # Do NOT collapse every commit OSError to 409. Four distinct outcomes:
    #   schema/version mismatch  -> 400 (can never succeed on retry)
    #   INCOMPATIBLE transaction -> 400, NON-RETRYABLE (spec) — re-WRITE, never re-commit
    #   genuine contention       -> 409 (the loser of a race can safely re-read + re-commit)
    #   raw store 5xx            -> 503 (ArrowIOError subclasses OSError) — an outage, not contention

    from catalog.services.dataplane import _classify_commit_error

    assert isinstance(
        _classify_commit_error(OSError("Append with different schema: fields did not match")),
        InvalidInputError,
    )
    assert isinstance(_classify_commit_error(OSError("All data files must have the same version")), InvalidInputError)
    # Incompatible => a NON-RETRYABLE client error, and the message must NOT invite a re-commit.
    incompatible = _classify_commit_error(OSError("Incompatible transaction: this Append is incompatible"))
    assert isinstance(incompatible, InvalidInputError)
    assert "NOT retryable" in str(incompatible)
    assert "re-commit" not in str(incompatible).replace("do not re-commit", "")
    # A genuine race, though, IS retryable — the loser re-reads and re-commits.
    assert isinstance(
        _classify_commit_error(OSError("commit conflict: concurrent writer won")),
        ConcurrentModificationError,
    )
    # An append to a never-created / declared-only table (no committed base) is a client error (400), NOT a
    # 503 the client would retry forever with the same read_version.
    assert isinstance(
        _classify_commit_error(OSError("Version 0 must already exist unless the operation is Overwrite")),
        InvalidInputError,
    )
    # A genuine object-store outage must NOT be mislabeled a client conflict.
    assert isinstance(
        _classify_commit_error(OSError("error performing PUT 503 to rustfs: Service Unavailable")),
        ServiceUnavailableError,
    )


def test_vending_mode_requires_sts_endpoint_fail_closed() -> None:
    # A token-egress guard: web_identity/sts without an STS endpoint would POST the caller's token to the
    # PUBLIC AWS STS endpoint — the config must fail closed at boot instead.
    from catalog.core.config import Settings

    base = {"s3_access_key_id": "x", "s3_secret_access_key": "x"}
    with pytest.raises(ValueError, match="LANCE_S3_STS_ENDPOINT"):
        Settings.model_validate({**base, "vending_mode": "web_identity"})
    ok = Settings.model_validate({**base, "vending_mode": "web_identity", "s3_sts_endpoint": "http://sts:9000"})
    assert ok.vending_mode == "web_identity"
