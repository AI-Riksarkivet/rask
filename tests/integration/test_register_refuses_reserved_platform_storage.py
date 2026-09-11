"""`register_table` must refuse a location in reserved platform storage (LH-027).

THE DOOR IT MIRRORS. `warehouses.py` refuses a warehouse whose bucket is reserved — the catalog root,
the registry, the medallion zone buckets — because claiming one makes that project the bucket's owner
and a later project-policy set then governs every tenant's data inside it (the 2026-07-23 Mallory
audit). `register_table` attaches a caller-supplied LOCATION and never looked at it, so the same
takeover was available through the other door: register a table at `s3://<catalog-root>/...`, receive
ownership tuples for it, and hold a governed handle on platform storage.

IT IS NOT THE SAME AS THE WAREHOUSE CASE AND IS WORSE IN ONE WAY: a warehouse claim at least creates a
registry record an operator can see. A registered table is one row in a manifest.

WHAT THIS DOES NOT ASSERT: that register refuses a location outside the namespace's own root. That is
the other half of the original row and it is deliberately not closed here — an external location is
the whole POINT of register (`deregister` keeps bytes precisely because they are not ours), so a
containment rule needs its own decision about what "outside" may mean.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def reserved_bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("LANCE_RESERVED_BUCKETS", "platform-secrets")
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    yield "platform-secrets"
    get_settings.cache_clear()


def test_registering_into_a_reserved_bucket_is_refused(real_ns_client: TestClient, reserved_bucket: str) -> None:
    """The refusal the warehouse door already makes, at the door that had none."""
    resp = real_ns_client.post(
        "/v1/table/acme$attached/register",
        json={"id": ["acme", "attached"], "location": f"s3://{reserved_bucket}/stolen"},
    )

    assert resp.status_code == 400, f"a reserved-bucket location was not refused: {resp.status_code} {resp.text[:200]}"
    assert reserved_bucket in resp.json().get("detail", ""), "the refusal must NAME the bucket so the caller can fix it"


def test_the_refusal_happens_before_the_native_call(real_ns_client: TestClient, reserved_bucket: str) -> None:
    """A guard that runs after the attach leaves a real manifest row behind to clean up.

    The estate's own check order — identity, shape, parent, authz, conflict, THEN the native write —
    exists for exactly this: rejecting afterwards means compensating, and a compensation can fail.
    """
    real_ns_client.post(
        "/v1/table/acme$attached2/register",
        json={"id": ["acme", "attached2"], "location": f"s3://{reserved_bucket}/stolen"},
    )

    described = real_ns_client.get("/v1/table/acme$attached2")
    assert described.status_code == 404, "the refused register still left the table attached"


def test_an_ordinary_location_is_still_accepted(real_ns_client: TestClient, reserved_bucket: str) -> None:
    """The guard must not refuse the case register EXISTS for — attaching external bytes."""
    resp = real_ns_client.post(
        "/v1/table/acme$ordinary/register",
        json={"id": ["acme", "ordinary"], "location": "s3://a-tenants-own-bucket/data"},
    )

    assert resp.status_code != 400 or reserved_bucket not in resp.text, f"an ordinary location was caught by the guard: {resp.text[:200]}"
