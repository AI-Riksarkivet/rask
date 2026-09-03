"""Which credential signed a write is observable in the logs, per table.

The whole point of vending is that bytes on object storage are signed by a credential scoped to one
table prefix instead of a key that reaches the whole bucket. Whether that is actually happening was
invisible: the vend succeeded silently, and every non-answer degraded to the ambient credential —
also by design — so a deployment whose vending door was misconfigured, whose catalog seam had no
vending capability, or whose chunks carried no namespace looked exactly like a deployment where every
write was scoped. An operator had no way to tell a hardened estate from one that had silently stopped
being hardened, which makes the posture unauditable.

ONE LINE PER TABLE, not per write. The cache exists because an ingest run has millions of units; a log
line on the hot path would be as wrong as a catalog round trip there. The transition is what carries
the information, so it is logged where the transition happens — the vend — and a refresh is a
transition too: a run long enough to re-vend should say so, or a credential that silently stopped
refreshing looks identical to one that never needed to.
"""

from __future__ import annotations

import logging

import pytest

from ingest.catalog_service import VendedCredential
from ingest.credentials import VendedCredentialCache


def test_a_vended_credential_is_reported_with_the_table_it_is_scoped_to(caplog: pytest.LogCaptureFixture) -> None:
    now = [1000.0]
    cache = VendedCredentialCache(
        lambda namespace, dataset, *, tier="write": VendedCredential(options={"aws_access_key_id": "AK"}, expires_at_millis=(now[0] + 900) * 1000),
        now=lambda: now[0],
    )
    with caplog.at_level(logging.INFO, logger="ingest.credentials"):
        cache.storage_options("acme-bronze", "events")

    records = [r for r in caplog.records if r.name == "ingest.credentials"]
    assert records, "a scoped credential was taken into use and nothing said so"
    assert "acme-bronze" in records[0].getMessage() and "events" in records[0].getMessage()


def test_the_hot_path_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    """A cached hit logs nothing — a line per write would be a line per million."""
    now = [1000.0]
    cache = VendedCredentialCache(
        lambda namespace, dataset, *, tier="write": VendedCredential(options={"aws_access_key_id": "AK"}, expires_at_millis=(now[0] + 900) * 1000),
        now=lambda: now[0],
    )
    cache.storage_options("acme-bronze", "events")
    with caplog.at_level(logging.INFO, logger="ingest.credentials"):
        # The first vend above legitimately logged. `caplog` collects every propagated record whether
        # or not it was emitted inside this block, so without clearing, this asserts on the setup call
        # and passes or fails depending on the root level another test happened to leave behind.
        caplog.clear()
        for _ in range(50):
            cache.storage_options("acme-bronze", "events")
    assert [r for r in caplog.records if r.name == "ingest.credentials"] == []


def test_a_refresh_says_so(caplog: pytest.LogCaptureFixture) -> None:
    now = [1000.0]
    cache = VendedCredentialCache(
        lambda namespace, dataset, *, tier="write": VendedCredential(options={"aws_access_key_id": "AK"}, expires_at_millis=(now[0] + 900) * 1000),
        now=lambda: now[0],
    )
    cache.storage_options("acme-bronze", "events")
    now[0] += 900
    with caplog.at_level(logging.INFO, logger="ingest.credentials"):
        caplog.clear()
        cache.storage_options("acme-bronze", "events")
    assert [r for r in caplog.records if r.name == "ingest.credentials"], "a re-vend after expiry was silent"


def test_falling_back_to_the_ambient_credential_is_reported_as_the_downgrade_it_is(caplog: pytest.LogCaptureFixture) -> None:
    """`None` is a supported posture (`mode_b`) AND the degradation path for a broken vending door.
    Both write with a key that reaches the whole bucket, so both must be visible — the caller cannot
    distinguish them and neither can a reader who is only shown silence."""
    cache = VendedCredentialCache(lambda namespace, dataset, *, tier="write": None, now=None)
    with caplog.at_level(logging.INFO, logger="ingest.credentials"):
        assert cache.storage_options("acme-bronze", "events") is None

    records = [r for r in caplog.records if r.name == "ingest.credentials"]
    assert records, "the write fell back to the ambient credential and nothing said so"
    assert "acme-bronze" in records[0].getMessage()


def test_the_secret_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A log line naming the credential would put a live secret in the log store — the exact thing
    scoping it was meant to avoid."""
    cache = VendedCredentialCache(
        lambda namespace, dataset, *, tier="write": VendedCredential(
            options={"aws_access_key_id": "AKIAEXAMPLE", "aws_secret_access_key": "s3cr3t-value", "session_token": "tok3n-value"},
            expires_at_millis=2_000_000.0,
        ),
        now=lambda: 1000.0,
    )
    with caplog.at_level(logging.INFO, logger="ingest.credentials"):
        cache.storage_options("acme-bronze", "events")

    for record in caplog.records:
        message = record.getMessage() + repr(getattr(record, "__dict__", {}))
        assert "s3cr3t-value" not in message
        assert "tok3n-value" not in message
