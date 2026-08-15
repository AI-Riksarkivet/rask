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
    StaticPrefixVendor,
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


def test_build_session_policy_read_vs_write() -> None:
    read: Any = build_session_policy("bkt", "tables/db1$users", "read")
    write: Any = build_session_policy("bkt", "tables/db1$users", "write")
    read_objs = read["Statement"][1]
    write_objs = write["Statement"][1]
    assert "s3:GetObject" in read_objs["Action"]
    assert "s3:PutObject" not in read_objs["Action"]
    assert "s3:PutObject" in write_objs["Action"]
    assert "s3:DeleteObject" in write_objs["Action"]
    # object actions scoped to exactly the table prefix
    assert read_objs["Resource"] == "arn:aws:s3:::bkt/tables/db1$users/*"
    list_stmt = read["Statement"][0]
    assert list_stmt["Condition"]["StringLike"]["s3:prefix"] == ["tables/db1$users/*"]


def test_build_session_policy_root_prefix() -> None:
    pol: Any = build_session_policy("bkt", "", "read")
    assert pol["Statement"][1]["Resource"] == "arn:aws:s3:::bkt/*"
    assert pol["Statement"][0]["Condition"]["StringLike"]["s3:prefix"] == ["*"]


def test_mode_b_vendor_returns_none() -> None:
    assert ModeBVendor().vend(table_location="s3://b/t", tier="read") is None


def test_static_prefix_vendor() -> None:
    vendor = StaticPrefixVendor({"b": {"access_key_id": "AK", "secret_access_key": "SK"}})
    out = vendor.vend(table_location="s3://b/t", tier="write")
    assert out is not None
    assert out.storage_options["access_key_id"] == "AK"
    assert out.expires_at_millis is None
    # unknown bucket -> None (caller falls back to Mode B)
    assert vendor.vend(table_location="s3://other/t", tier="read") is None


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
    assert opts["access_key_id"] == "AK"
    assert opts["secret_access_key"] == "SK"
    assert opts["session_token"] == "TK"
    assert opts["endpoint"] == "http://minio:9000"
    assert out.expires_at_millis is not None and out.expires_at_millis > 0
    # the inline session policy was passed, scoped to the table prefix + write actions
    assert captured["DurationSeconds"] == 900
    assert "tables/t1" in captured["Policy"]
    assert "s3:PutObject" in captured["Policy"]


def test_make_vendor_selection() -> None:
    assert isinstance(make_vendor("mode_b"), ModeBVendor)
    assert isinstance(make_vendor("static"), StaticPrefixVendor)
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
    assert creds.storage_options["session_token"] == "ST"
    assert creds.storage_options["endpoint"] == "http://rustfs:9000"
    assert creds.expires_at_millis is not None and creds.expires_at_millis > 0
    assert captured["WebIdentityToken"] == "the.jwt.tok"
    policy = json.loads(captured["Policy"])
    actions = policy["Statement"][1]["Action"]
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
    assert opts["access_key_id"] and opts["secret_access_key"] and opts["session_token"]
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


@pytest.mark.parametrize("tier", ["read", "write"])
def test_a_tenants_policy_denies_another_tenants_bucket(tier: Tier) -> None:
    """THE #74 claim, offline: the credential vended for tenant B's table must not reach tenant A's
    bucket at all — not for GET, not for PUT, not even to LIST it."""
    policy: Any = build_session_policy("tenant-b", "isobns/isobtbl.lance", tier)
    assert not _policy_allows(policy, action="s3:GetObject", bucket="tenant-a", key="isoans/isoatbl.lance/data/x.lance")
    assert not _policy_allows(policy, action="s3:PutObject", bucket="tenant-a", key="isoans/isoatbl.lance/data/x.lance")
    assert not _policy_allows(policy, action="s3:ListBucket", bucket="tenant-a", key="")


@pytest.mark.parametrize("tier", ["read", "write"])
def test_a_tenants_policy_still_allows_its_OWN_table(tier: Tier) -> None:
    """The negative twin: a policy that denied everything would satisfy the test above while
    breaking the product, so pin that B keeps reaching B."""
    policy: Any = build_session_policy("tenant-b", "isobns/isobtbl.lance", tier)
    assert _policy_allows(policy, action="s3:GetObject", bucket="tenant-b", key="isobns/isobtbl.lance/data/x.lance")
    assert _policy_allows(policy, action="s3:ListBucket", bucket="tenant-b", key="")


def test_a_policy_does_not_reach_a_SIBLING_table_in_the_same_bucket() -> None:
    """Single-bucket deployments share one bucket across tables, so the prefix — not just the
    bucket — is the boundary. A credential for one table must not read its neighbour."""
    policy: Any = build_session_policy("shared", "nsa/tbl_a.lance", "write")
    assert not _policy_allows(policy, action="s3:GetObject", bucket="shared", key="nsa/tbl_b.lance/data/x.lance")
    assert _policy_allows(policy, action="s3:GetObject", bucket="shared", key="nsa/tbl_a.lance/data/x.lance")


def test_a_read_tier_policy_cannot_write_its_own_table() -> None:
    """The tier split is a security boundary, not an ergonomic one."""
    policy: Any = build_session_policy("tenant-b", "isobns/isobtbl.lance", "read")
    assert not _policy_allows(policy, action="s3:PutObject", bucket="tenant-b", key="isobns/isobtbl.lance/data/x.lance")
    assert not _policy_allows(policy, action="s3:DeleteObject", bucket="tenant-b", key="isobns/isobtbl.lance/data/x.lance")


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

#: The exact keys a client hands to its object-store driver. Lance-style and BARE, because
#: `DescribeTableResponse.storage_options` is documented as passed straight to Lance — so the
#: catalog cannot rename them for boto3's convenience without breaking every Lance reader.
_REQUIRED_STORAGE_OPTION_KEYS = frozenset({"access_key_id", "secret_access_key", "session_token", "region"})


def _fake_creds(**kwargs: Any) -> dict[str, Any]:
    return {
        "Credentials": {
            "AccessKeyId": "AK",
            "SecretAccessKey": "SK",
            "SessionToken": "ST",
            "Expiration": dt.datetime(2030, 1, 1, tzinfo=dt.UTC),
        }
    }


def _vended_options(vendor_name: str) -> dict[str, str]:
    if vendor_name == "sts":
        vendor: Any = StsVendor(role_arn="arn:aws:iam::1:role/r", region="us-east-1", ttl_seconds=900, assume_role=_fake_creds)
        out = vendor.vend(table_location="s3://bkt/tables/t1", tier="read")
    else:
        vendor = WebIdentityVendor(region="us-east-1", endpoint="http://rustfs:9000", assume=_fake_creds)
        out = vendor.vend(table_location="s3://bkt/tables/t1", tier="read", web_identity_token="the.jwt.tok")
    assert out is not None
    return out.storage_options


@pytest.mark.parametrize("vendor_name", ["sts", "web_identity"])
def test_every_vendor_emits_the_same_bare_storage_option_keys(vendor_name: str) -> None:
    """All credential-issuing vendors agree on ONE key vocabulary.

    Parametrized over the vendors rather than asserted once: `web_identity` is the only RustFS-viable
    mode while `sts` is the one most of this file drives, so a divergence between them would be
    invisible to both. A client cannot be expected to sniff which vendor a deployment configured.
    """
    options = _vended_options(vendor_name)
    missing = _REQUIRED_STORAGE_OPTION_KEYS - set(options)
    assert not missing, f"{vendor_name} stopped emitting {sorted(missing)} — every Lance client reads these by name"
    # NO `aws_`-prefixed alias, in either direction. The e2e's bug was reading boto3's PARAMETER
    # names back out of the server's payload; adding an alias here would make that mistake work by
    # accident, and the next one would be just as invisible.
    assert not [k for k in options if k.startswith("aws_")], options


def test_the_isolation_e2e_reads_the_keys_the_vendors_actually_emit() -> None:
    """The e2e is env-gated and SKIPS by default, so this is what keeps its client wiring honest.

    It reads the e2e's own source and asserts that every key `_client` pulls out of the vended
    options is one a vendor emits. Parsing source is a blunt instrument; the alternative is importing
    a module that hard-requires a deployed stack, and the failure being guarded — a rename applied on
    one side only — is exactly what a green-by-skipping suite cannot report.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "e2e-py" / "test_credential_isolation_e2e.py"
    body = src.read_text()
    client = body[body.index("def _client(") :]
    client = client[: client.index("\n\n\n")]
    read = set(re.findall(r"""creds(?:\.get)?\(?\[?["']([a-z_]+)["']\]?\)?""", client))
    assert read, "could not parse the e2e's credential reads — update this pin rather than deleting it"
    unknown = read - _REQUIRED_STORAGE_OPTION_KEYS - {"endpoint"}
    assert not unknown, f"the isolation e2e reads {sorted(unknown)}, which no vendor emits (diff2 F5)"
