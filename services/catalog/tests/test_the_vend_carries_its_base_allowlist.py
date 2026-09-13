"""The base-sanction allowlist must survive the trip from settings to the rendered STS policy.

`build_session_policy` decides which declared bases are granted, and every other test of that decision
calls it DIRECTLY with an explicit `sanctioned_bases=`. That pins the rule and nothing that carries it:
delete `sanctioned_bases=self._sanctioned_bases` from a vendor, or
`sanctioned_bases=settings.multibase_data_base_list` from the catalog's lifespan, and the parameter
falls back to its fail-closed empty default — every operator-allowlisted foreign base is dropped from
every real vend, a multi-base table's credential stops reaching its own data (the §H12 compaction
refusals the grant was added to fix return), and not one test goes red.

So these drive the SEAM rather than the rule: a real `StsVendor` built the way `make_vendor` builds it,
vending through an injected `assume_role` that captures the policy document the store would receive.
The rule itself is covered by
`services/catalog/tests/test_a_declared_base_cannot_reach_a_table_the_caller_never_opened.py`; nothing
here re-tests it.
"""

from __future__ import annotations

import json
from typing import Any

from catalog.core.vending import StsVendor, make_vendor


TABLE = "s3://lakehouse/acme-wh/mine$t"
FOREIGN = "s3://data-bases/acme"


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


def test_a_vendor_carries_its_allowlist_into_the_rendered_policy() -> None:
    """THE GATE. Without the vendor forwarding its allowlist, this base is silently dropped."""
    sink: dict[str, Any] = {}
    vendor = StsVendor(
        role_arn="arn:aws:iam::000000000000:role/lance-vend",
        region="us-east-1",
        assume_role=_capturing_assume(sink),
        access_key="unit",
        secret_key="unit",
        sanctioned_bases=(FOREIGN,),
    )

    vendor.vend(table_location=TABLE, tier="read", bases=(FOREIGN,))

    assert _base_resources(sink["policy"]) == ["arn:aws:s3:::data-bases/acme/*"], (
        "the vendor did not carry its sanctioned-base allowlist into build_session_policy"
    )


def test_a_vendor_with_no_allowlist_drops_the_same_base() -> None:
    """The negative twin, so the test above cannot pass for the wrong reason — if the allowlist were
    ignored entirely and every base granted, this would fail."""
    sink: dict[str, Any] = {}
    vendor = StsVendor(
        role_arn="arn:aws:iam::000000000000:role/lance-vend",
        region="us-east-1",
        assume_role=_capturing_assume(sink),
        access_key="unit",
        secret_key="unit",
    )

    vendor.vend(table_location=TABLE, tier="read", bases=(FOREIGN,))

    assert _base_resources(sink["policy"]) == [], "an unsanctioned foreign base must not be granted"


def test_the_factory_hands_the_allowlist_to_the_vendor_it_builds() -> None:
    """`make_vendor` is what the catalog's lifespan calls, so a vendor built any other way in a test
    would not prove the deployed path carries the list."""
    sink: dict[str, Any] = {}
    vendor = make_vendor(
        "sts",
        assume_role_arn="arn:aws:iam::000000000000:role/lance-vend",
        access_key="unit",
        secret_key="unit",
        sanctioned_bases=[FOREIGN],
    )
    assert isinstance(vendor, StsVendor)
    vendor._assume_role = _capturing_assume(sink)  # noqa: SLF001 — the injection point make_vendor does not expose

    vendor.vend(table_location=TABLE, tier="read", bases=(FOREIGN,))

    assert _base_resources(sink["policy"]) == ["arn:aws:s3:::data-bases/acme/*"]
