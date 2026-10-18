"""A vendor built with no allowlist drops a foreign base from the session policy it renders.

That the deployed allowlist reaches the lifespan-built vendor, and a table's own directory beneath it is
granted, is driven through the credentials door by
`test_a_vend_for_one_table_grants_no_sibling_on_its_data_base.py`. The rule itself is covered by
`test_a_declared_base_cannot_reach_a_table_the_caller_never_opened.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from catalog.core.vending import WebIdentityVendor
from service_kit.governed.store_credential import S3KeyPair
from service_kit.governed.store_session import IssuedSession, WebIdentity


TABLE = "s3://lakehouse/acme-wh/mine$t"
FOREIGN = "s3://data-bases/acme"
#: One table's own directory beneath the sanctioned base — what a create registers ([[LH-252]]).
TABLE_DIRECTORY = f"{FOREIGN}/t-1"


def _capturing_exchange(sink: dict[str, Any]):
    def _exchange(_identity: WebIdentity, *, session_name: str, duration_seconds: int, policy: str | None = None) -> IssuedSession:
        sink["policy"] = json.loads(str(policy))
        return IssuedSession(
            key_pair=S3KeyPair(access_key_id="AK", secret_access_key=SecretStr("SK"), session_token=SecretStr("ST")),
            expiration=datetime(2030, 1, 1, tzinfo=UTC),
        )

    return _exchange


def _base_resources(policy: dict[str, Any]) -> list[str]:
    statements = policy["Statement"]
    assert isinstance(statements, list)
    return [str(s.get("Resource")) for s in statements if isinstance(s, dict) and str(s.get("Sid", "")).startswith("BaseObjects")]


def test_a_vendor_with_no_allowlist_drops_the_same_base() -> None:
    """If the allowlist were ignored entirely and every base granted, this would fail."""
    sink: dict[str, Any] = {}
    identity = WebIdentity(token_file=Path("projected-token"), endpoint=None, region="us-east-1", role_arn="arn:aws:iam::000000000000:role/lance-vend")
    vendor = WebIdentityVendor(identity=identity, exchange=_capturing_exchange(sink))

    vendor.vend(table_location=TABLE, tier="read", session_name="reader", bases=(TABLE_DIRECTORY,))

    assert _base_resources(sink["policy"]) == [], "an unsanctioned foreign base must not be granted"
