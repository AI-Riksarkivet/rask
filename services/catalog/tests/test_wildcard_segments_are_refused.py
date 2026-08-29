"""A `*`/`?` in a table/namespace segment must be refused at the create door (CAT-CORE-02).

The segment name becomes the table's object-store prefix, and that prefix is interpolated UNESCAPED
into the STS inline session policy that vends per-table credentials
(`core.vending.build_session_policy`). `*` and `?` are IAM wildcards inside both a Resource ARN and an
`s3:prefix` StringLike condition, and IAM offers no way to escape them there — so a table named `foo*`
would be handed credentials scoped to every sibling object under `foo*` (`bucket/foo*/*` matches
`foobar`). The fix is input rejection at the create door plus a defensive refusal in the policy builder.
"""

from __future__ import annotations

from typing import cast

import pytest
from catalog.core.identifiers import require_safe_segments
from catalog.core.vending import build_session_policy
from lance_namespace import InvalidInputError


def test_policy_builder_refuses_a_wildcard_prefix_rather_than_widen_the_grant() -> None:
    # Today this returns a policy whose object Resource is `arn:aws:s3:::b/ns/foo*/*` — a wildcard that
    # matches every sibling under `foo`, exactly the widening the vendor must never emit.
    with pytest.raises(ValueError, match=r"[*?]"):
        build_session_policy("b", "ns/foo*", "read")

    with pytest.raises(ValueError, match=r"[*?]"):
        build_session_policy("b", "ns/bar?", "write")


def test_policy_builder_still_scopes_a_clean_prefix() -> None:
    policy = build_session_policy("b", "ns/pages", "read")
    # The builder's return type is `dict[str, object]`; "Statement" is by IAM-policy construction a
    # list of statement dicts, so narrow it here instead of suppressing the checker.
    statements = cast("list[dict[str, object]]", policy["Statement"])
    obj = next(s for s in statements if s["Sid"] == "TableObjects")
    assert obj["Resource"] == "arn:aws:s3:::b/ns/pages/*"


def test_a_wildcard_segment_is_refused_at_the_door() -> None:
    with pytest.raises(InvalidInputError, match=r"foo\*"):
        require_safe_segments(["foo*"], delimiter="$")

    with pytest.raises(InvalidInputError, match=r"bar\?"):
        require_safe_segments(["ns", "bar?"], delimiter="$")


def test_a_clean_identifier_passes_the_door() -> None:
    require_safe_segments(["acme-bronze", "pages"], delimiter="$")
