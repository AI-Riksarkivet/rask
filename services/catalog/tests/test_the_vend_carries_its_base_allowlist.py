"""A vendor built with no allowlist drops a foreign base from the STS policy it renders.

That the deployed allowlist reaches the lifespan-built vendor, and a table's own directory beneath it is
granted, is driven through the credentials door by
`test_a_vend_for_one_table_grants_no_sibling_on_its_data_base.py`. The rule itself is covered by
`test_a_declared_base_cannot_reach_a_table_the_caller_never_opened.py`.
"""

from __future__ import annotations

import json
from typing import Any

from catalog.core.vending import StsVendor


TABLE = "s3://lakehouse/acme-wh/mine$t"
FOREIGN = "s3://data-bases/acme"
#: One table's own directory beneath the sanctioned base — what a create registers ([[LH-252]]).
TABLE_DIRECTORY = f"{FOREIGN}/t-1"


def _capturing_assume(sink: dict[str, Any]):
    def _assume(**kwargs: Any) -> dict[str, Any]:
        sink["policy"] = json.loads(str(kwargs["Policy"]))
        return {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "ST",
                "Expiration": None,
            }
        }

    return _assume


def _base_resources(policy: dict[str, Any]) -> list[str]:
    statements = policy["Statement"]
    assert isinstance(statements, list)
    return [str(s.get("Resource")) for s in statements if isinstance(s, dict) and str(s.get("Sid", "")).startswith("BaseObjects")]


def test_a_vendor_with_no_allowlist_drops_the_same_base() -> None:
    """If the allowlist were ignored entirely and every base granted, this would fail."""
    sink: dict[str, Any] = {}
    vendor = StsVendor(
        role_arn="arn:aws:iam::000000000000:role/lance-vend",
        region="us-east-1",
        assume_role=_capturing_assume(sink),
        access_key="unit",
        secret_key="unit",
    )

    vendor.vend(table_location=TABLE, tier="read", bases=(TABLE_DIRECTORY,))

    assert _base_resources(sink["policy"]) == [], "an unsanctioned foreign base must not be granted"
