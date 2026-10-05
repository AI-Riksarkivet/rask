"""Unit tests for the pluggable credential vendor (catalog.core.vending).

No network: the STS path is exercised with an injected fake ``assume_role`` so
the session-policy scoping + storage_options assembly are pinned without boto3.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from catalog.core.vending import (
    ModeBVendor,
    StsVendor,
    Tier,
    WebIdentityVendor,
    build_session_policy,
    make_vendor,
    split_s3_location,
)


def test_split_s3_location() -> None:
    assert split_s3_location("s3://bucket/a/b/c") == ("bucket", "a/b/c")
    assert split_s3_location("s3://bucket") == ("bucket", "")
    with pytest.raises(ValueError):
        split_s3_location("/no/bucket")


def test_build_session_policy_root_prefix() -> None:
    pol: Any = build_session_policy("bkt", "", "read")
    assert pol["Statement"][1]["Resource"] == "arn:aws:s3:::bkt/*"
    assert pol["Statement"][0]["Condition"]["StringLike"]["s3:prefix"] == ["*"]


def test_sts_vendor_with_fake_assume_role() -> None:
    captured: dict[str, Any] = {}

    def fake_assume_role(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "TK",
                "Expiration": dt.datetime(2030, 1, 1, tzinfo=dt.UTC),
            }
        }

    vendor = StsVendor(
        role_arn="arn:aws:iam::1:role/r",
        region="us-east-1",
        endpoint="http://minio:9000",
        ttl_seconds=900,
        assume_role=fake_assume_role,
    )
    out = vendor.vend(table_location="s3://bkt/tables/t1", tier="write")
    assert out is not None
    opts = out.storage_options
    assert opts["aws_access_key_id"] == "AK"
    assert opts["aws_secret_access_key"] == "SK"
    assert opts["aws_session_token"] == "TK"
    assert opts["endpoint"] == "http://minio:9000"
    assert out.expires_at_millis is not None and out.expires_at_millis > 0
    # the inline session policy was passed, scoped to the table prefix + write actions
    assert captured["DurationSeconds"] == 900
    assert "tables/t1" in captured["Policy"]
    assert "s3:PutObject" in captured["Policy"]


def test_make_vendor_selection() -> None:
    assert isinstance(make_vendor("mode_b"), ModeBVendor)
    assert isinstance(make_vendor("sts", assume_role_arn="arn:aws:iam::1:role/r"), StsVendor)


def test_make_vendor_sts_requires_arn() -> None:
    with pytest.raises(ValueError):
        make_vendor("sts")


def test_web_identity_vendor_exchanges_the_token_for_scoped_creds() -> None:
    captured: dict[str, Any] = {}

    def _fake_assume(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "ST",
                "Expiration": dt.datetime(2030, 1, 1, tzinfo=dt.UTC),
            }
        }

    vendor = WebIdentityVendor(region="us-east-1", endpoint="http://rustfs:9000", assume=_fake_assume)
    # No caller token → nothing to exchange → fall back to server-mediated.
    assert vendor.vend(table_location="s3://b/t", tier="read", web_identity_token=None) is None
    # With the caller's token → scoped creds; the token + a write-tier session policy are forwarded.
    creds = vendor.vend(table_location="s3://lance-catalog/db$t", tier="write", web_identity_token="the.jwt.tok")
    assert creds is not None
    assert creds.storage_options["aws_session_token"] == "ST"
    assert creds.storage_options["endpoint"] == "http://rustfs:9000"
    assert creds.expires_at_millis is not None and creds.expires_at_millis > 0
    assert captured["WebIdentityToken"] == "the.jwt.tok"
    policy = json.loads(captured["Policy"])
    actions = [action for statement in policy["Statement"] for action in statement["Action"]]
    assert "s3:PutObject" in actions  # write tier scoped to the table prefix


def test_web_identity_vendor_propagates_a_rejected_exchange() -> None:
    """A rejected token exchange must PROPAGATE (the endpoint maps it to 4xx), not return None/garbage."""
    from botocore.exceptions import ClientError

    def _boom(**_kwargs: object) -> dict[str, object]:
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "AssumeRoleWithWebIdentity")

    vendor = WebIdentityVendor(region="us-east-1", assume=_boom)
    with pytest.raises(ClientError):
        vendor.vend(table_location="s3://b/t", tier="read", web_identity_token="bad.jwt")


def test_make_vendor_builds_web_identity() -> None:
    assert isinstance(make_vendor("web_identity"), WebIdentityVendor)


def test_sts_vendor_against_a_real_assume_role_implementation() -> None:
    """End-to-end against moto's STS (a real AssumeRole impl) — proves the default boto3 path vends valid
    scoped creds, not just the injected-fake path. (RustFS's STS can't AssumeRole — it needs WebIdentity —
    so a compliant STS like moto/MinIO/AWS/Ceph is what exercises the live boto3 client.)"""
    moto = pytest.importorskip("moto")
    with moto.mock_aws():
        vendor = StsVendor(role_arn="arn:aws:iam::123456789012:role/lance-vend", region="us-east-1", ttl_seconds=900)
        creds = vendor.vend(table_location="s3://lance-catalog/db$users", tier="read")
    assert creds is not None
    opts = creds.storage_options
    assert opts["aws_access_key_id"] and opts["aws_secret_access_key"] and opts["aws_session_token"]
    assert opts["region"] == "us-east-1"
    assert creds.expires_at_millis is not None


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
