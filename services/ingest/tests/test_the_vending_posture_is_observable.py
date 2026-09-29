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

from service_kit.lakehouse.vended_credentials import VendedCredential, VendedCredentialCache


def test_a_vended_credential_is_reported_with_the_table_it_is_scoped_to(caplog: pytest.LogCaptureFixture) -> None:
    now = [1000.0]
    cache = VendedCredentialCache(
        lambda namespace, dataset, *, tier="write": VendedCredential(options={"aws_access_key_id": "AK"}, expires_at_millis=(now[0] + 900) * 1000),
        now=lambda: now[0],
    )
    with caplog.at_level(logging.INFO, logger="service_kit.lakehouse.vended_credentials"):
        cache.storage_options("acme-bronze", "events")

    records = [r for r in caplog.records if r.name == "service_kit.lakehouse.vended_credentials"]
    assert records, "a scoped credential was taken into use and nothing said so"
    assert "acme-bronze" in records[0].getMessage() and "events" in records[0].getMessage()
