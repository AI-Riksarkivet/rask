"""`register_table` is ROOT-RELATIVE by construction, and the reserved bucket must stay reachable.

THIS FILE ASSERTED THE OPPOSITE FOR HALF A DAY. It pinned a guard that refused a registration whose
location bucket was reserved platform storage — reasoning by analogy from `warehouses.py`, which
refuses a WAREHOUSE over a reserved bucket. The analogy is wrong, and `rask-lance-catalog` names the
error exactly: the reserved bucket blocks the WAREHOUSE route (a tenant claiming platform storage,
where `provision_bucket` is idempotent so the claim silently succeeds, the project becomes the
bucket's owner, and a later project-policy set governs every tenant's data in it) while leaving the
REGISTRATION route open (naming one individual dataset, which makes nobody an owner of anything).
"Conflating them is how you conclude the cascade can never be governed" — and the cascade head
registers its bronze seed into precisely that bucket.

The guard was also inert. `register_table` addresses a location inside the root it is connected to and
nowhere else (`catalog_register.relative_location`), every caller sends a relative path — undrop sends
the final path segment alone — and the backend refuses an absolute URI outright. So it was a control
that could not fire, whose only effect would have arrived the day absolute URIs became valid, by
closing a route the design deliberately leaves open.

What is worth pinning is the property that makes the whole question moot: locations here are relative,
and the platform's own bucket is registrable.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def reserved_bucket() -> Iterator[str]:
    """The estate's real reserved bucket is the catalog root — the one the cascade head registers into."""
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    yield next(iter(sorted(get_settings().reserved_bucket_set)), "lance-catalog")
    get_settings.cache_clear()


def test_a_registration_is_not_refused_for_naming_platform_storage(real_ns_client: TestClient, reserved_bucket: str) -> None:
    """The deliberately-open route: the cascade head registers its bronze seed in the reserved bucket.

    A 400 whose message mentions the bucket would mean the warehouse rule has leaked onto this door.
    Any other status is this test's business to ignore — a missing parent or an absent location is a
    different door's answer, and pinning them here would make this a test of unrelated behaviour.
    """
    resp = real_ns_client.post(
        "/v1/table/medallion$seed/register",
        json={"id": ["medallion", "seed"], "location": "medallion/seed"},
    )

    refused_for_the_bucket = resp.status_code == 400 and reserved_bucket in resp.text
    assert not refused_for_the_bucket, f"the warehouse rule leaked onto the register door: {resp.text[:200]}"


def test_the_register_location_is_root_relative_by_construction() -> None:
    """Why a bucket check on this door can never fire: there is no bucket in the location.

    `relative_location` is the producer-side seam and it says so outright; the undrop paths send the
    final path segment alone. A location that carries a bucket is refused below this layer, so a guard
    reading one is reading a field that does not arrive.
    """
    from medallion.services.catalog_register import relative_location

    assert relative_location("s3://lance-catalog/medallion/bronze", "s3://lance-catalog") == "medallion/bronze"

    with pytest.raises(Exception) as caught:
        relative_location("s3://someone-elses-bucket/data", "s3://lance-catalog")
    assert "lance-catalog" in str(caught.value) or "someone-elses-bucket" in str(caught.value), (
        "a location outside the connection root must be refused NAMING both, so the caller can see which is which"
    )
