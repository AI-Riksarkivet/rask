"""The commit-error taxonomy the catalog's own commits share, and the catalog's fail-closed store identity.

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


@pytest.mark.parametrize(
    ("given", "refusal"),
    [
        pytest.param({"vending_mode": "web_identity"}, "LANCE_S3_WEB_IDENTITY_TOKEN_FILE", id="a-vend-with-no-token-to-exchange"),
        pytest.param({"s3_web_identity_token_file": "/t"}, "LANCE_S3_STS_ENDPOINT", id="a-token-sent-to-the-public-aws-sts"),
        pytest.param(
            {"s3_web_identity_token_file": "/t", "s3_sts_endpoint": "http://sts:9000", "s3_access_key_id": "x", "s3_secret_access_key": "x"},
            "second identity",
            id="a-key-pair-beside-the-token",
        ),
        pytest.param(
            {"secrets_from_dapr": True, "s3_access_key_id": "x", "s3_secret_access_key": "x"},
            "LANCE_S3_WEB_IDENTITY_TOKEN_FILE",
            id="a-key-pair-with-the-secret-store-on",
        ),
    ],
)
def test_the_catalogs_store_identity_fails_closed_at_boot(given: dict[str, object], refusal: str) -> None:
    """A catalog boots with exactly one store identity, and on the estate that is its ServiceAccount token (ADR 0170).

    Without an STS endpoint boto3 resolves the PUBLIC AWS STS endpoint and would POST the token there; a pair beside
    the token is a second identity; and a catalog reading its secrets from the store holds no pair of its own.
    """
    from catalog.core.config import Settings

    with pytest.raises(ValueError, match=refusal):
        Settings.model_validate(given)
