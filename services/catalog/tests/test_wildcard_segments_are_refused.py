"""The policy builder refuses a table prefix holding IAM policy syntax rather than widen or redirect the grant (CAT-CORE-02).

A table's prefix is interpolated into the STS inline session policy that vends per-table credentials
(`core.vending.build_session_policy`). `*` and `?` are IAM wildcards inside both a Resource ARN and an `s3:prefix`
StringLike condition, so a prefix `foo*` would scope credentials to every sibling object under it (`bucket/foo*/*`
matches `foobar`), and `${` opens a policy variable IAM substitutes as it evaluates ([[LH-526]]). No prefix the catalog
vends holds either: a table the catalog makes lives under its uuid, and the register door refuses a location holding
them. The refusal here is the builder's own, should a prefix ever arrive holding one.
"""

from __future__ import annotations

import pytest

from catalog.core.vending import build_session_policy


def test_policy_builder_refuses_a_prefix_holding_iam_policy_syntax() -> None:
    # Today this returns a policy whose object Resource is `arn:aws:s3:::b/ns/foo*/*` — a wildcard that
    # matches every sibling under `foo`, exactly the widening the vendor must never emit.
    with pytest.raises(ValueError, match=r"[*?]"):
        build_session_policy("b", "ns/foo*", "read")

    with pytest.raises(ValueError, match=r"[*?]"):
        build_session_policy("b", "ns/bar?", "write")

    with pytest.raises(ValueError, match=r"IAM policy syntax \['\$\{'\]"):
        build_session_policy("b", "ns/baz${aws:username}", "read")
