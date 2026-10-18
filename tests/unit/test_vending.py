"""Unit tests for the session policy a vend is narrowed by (catalog.core.vending).

No network: the policy document the vendor builds is evaluated directly. What the exchange sends and what the
door answers are driven through the real catalog app on a moto STS in
`services/catalog/tests/test_the_catalog_signs_only_with_sessions_its_token_is_exchanged_for.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from catalog.core.vending import Tier, build_session_policy, split_s3_location


def test_split_s3_location() -> None:
    assert split_s3_location("s3://bucket/a/b/c") == ("bucket", "a/b/c")
    assert split_s3_location("s3://bucket") == ("bucket", "")
    with pytest.raises(ValueError):
        split_s3_location("/no/bucket")


def test_build_session_policy_root_prefix() -> None:
    pol: Any = build_session_policy("bkt", "", "read")
    assert pol["Statement"][1]["Resource"] == "arn:aws:s3:::bkt/*"
    assert pol["Statement"][0]["Condition"]["StringLike"]["s3:prefix"] == ["*"]


# ---- #74 the cross-tenant attack, evaluated OFFLINE -------------------------------------------
# The e2e attack (tests/e2e-py/test_credential_isolation_e2e.py) is the real proof: it points a
# vended credential at another tenant's bucket and asks the STORE. It is env-gated, so it cannot
# guard the claim on every commit. These evaluate the same attack against the policy the vendor
# actually builds, using an IAM-semantics evaluator — no store, no mock of one, just the document.


def _policy_allows(policy: Any, *, action: str, bucket: str, key: str) -> bool:
    """Does this session policy ALLOW ``action`` on ``bucket/key``? IAM semantics, narrowed to what
    a session policy can express here: explicit Allow only (no Deny statements are emitted), and a
    ``*`` in a Resource arn matches any run of characters — the same wildcard the store applies."""
    import re

    target = f"arn:aws:s3:::{bucket}/{key}" if key else f"arn:aws:s3:::{bucket}"
    for stmt in policy["Statement"]:
        if stmt["Effect"] != "Allow" or action not in stmt["Action"]:
            continue
        pattern = "^" + ".*".join(re.escape(part) for part in stmt["Resource"].split("*")) + "$"
        if re.match(pattern, target):
            return True
    return False


@pytest.mark.parametrize("tier", ["read"])
def test_a_tenants_policy_denies_another_tenants_bucket(tier: Tier) -> None:
    """THE #74 claim, offline: the credential vended for tenant B's table must not reach tenant A's
    bucket at all — not for GET, not for PUT, not even to LIST it."""
    policy: Any = build_session_policy("tenant-b", "isobns/isobtbl.lance", tier)
    assert not _policy_allows(policy, action="s3:GetObject", bucket="tenant-a", key="isoans/isoatbl.lance/data/x.lance")
    assert not _policy_allows(policy, action="s3:PutObject", bucket="tenant-a", key="isoans/isoatbl.lance/data/x.lance")
    assert not _policy_allows(policy, action="s3:ListBucket", bucket="tenant-a", key="")


@pytest.mark.parametrize("tier", ["read"])
def test_a_tenants_policy_still_allows_its_OWN_table(tier: Tier) -> None:
    """The negative twin: a policy that denied everything would satisfy the test above while
    breaking the product, so pin that B keeps reaching B."""
    policy: Any = build_session_policy("tenant-b", "isobns/isobtbl.lance", tier)
    assert _policy_allows(policy, action="s3:GetObject", bucket="tenant-b", key="isobns/isobtbl.lance/data/x.lance")
    assert _policy_allows(policy, action="s3:ListBucket", bucket="tenant-b", key="")


# --------------------------------------------------------------------------- #
# diff2 F5 — the WIRE CONTRACT between what the vendors emit and what a client reads
#
# The tenant-isolation e2e read `body["credentials"]["aws_access_key_id"]` for its entire life:
# wrong NESTING (key material lives one level down, under `storage_options`) and wrong NAMES (`aws_`
# prefixes are boto3's own kwargs, never anything the server emits). Its first real run would have
# raised KeyError. It never ran — the suite is env-gated on a two-tenant deployed stack and skips by
# default — so a defect in the estate's headline isolation proof was found by reading, not failing.
#
# These pins live HERE, in a collected path, precisely because that e2e cannot be relied on to
# notice. They are the always-running half of the contract.
# --------------------------------------------------------------------------- #


def test_a_declared_base_is_granted_READ_even_at_the_maintain_tier() -> None:
    """A table whose fragments resolve through a base cannot use its credential without reading there.

    MEASURED LIVE 2026-09-08 (§ H12): the sweep vends a table-scoped write credential — the right
    mechanism — then probes a base the manifest declares. ACCESS_DENIED, so `probed=None`, so the
    compaction gate refuses. 69 datasets a tick, against a base the root credential shows is not a
    dataset root at all, i.e. every one of those refusals was false.

    READ and never WRITE, at either tier. The table reads THROUGH the base; it does not own it, and a
    write grant on another dataset's root is the blast radius the whole vending design exists to avoid.
    § C1: "read on inherited bases, write on `target_bases`, never on reference-only bases" — this is
    the first of the three, and the other two need `target_bases` evidence a manifest read does not yet
    provide.

    The base here sits OUTSIDE the table's prefix, so it reaches the policy only because the operator
    sanctioned it — the tier asymmetry is what this pins, and an unsanctioned base would be dropped
    before the question could be asked (`test_a_declared_base_cannot_reach_a_table_the_caller_never_opened`).
    """
    policy: Any = build_session_policy("bkt", "tables/db1$users", "maintain", bases=("s3://bkt/shared/src.lance",), sanctioned_bases=("s3://bkt/shared",))
    objects = [st for st in policy["Statement"] if st["Action"] != ["s3:ListBucket"]]

    table = next(st for st in objects if "tables/db1$users" in st["Resource"])
    base = next(st for st in objects if "shared/src.lance" in st["Resource"])

    assert "s3:PutObject" in table["Action"], "the table's own prefix keeps its maintain tier"
    assert base["Action"] == ["s3:GetObject"], f"a base must be readable and never writable: {base['Action']}"


def test_a_base_carrying_an_IAM_METACHARACTER_is_refused_like_a_prefix() -> None:
    """The prefix guard exists because `*`/`?` cannot be escaped inside a Resource ARN or an `s3:prefix`
    condition. A base path arrives off a MANIFEST rather than through the create doors'
    `require_safe_segments`, so it is the less trusted of the two and needs the guard more."""
    for hostile in ("s3://bkt/shared/*", "s3://bkt/sh?red/src.lance"):
        with pytest.raises(ValueError, match="wildcard metacharacter"):
            build_session_policy("bkt", "tables/db1$users", "read", bases=(hostile,))
