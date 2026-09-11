"""A manifest-declared base path grants READ; a base at a bucket root grants the bucket.

`build_session_policy` appends, for every base the table's manifest declares, a statement granting
READ on `<base_bucket>/<base_prefix>/*`. When the base carries no prefix the resource collapses to
`arn:aws:s3:::<bucket>/*`. Measured 2026-09-11:

    base "s3://lakehouse"            -> BaseObjects0 arn:aws:s3:::lakehouse/*
    base "s3://rask-observability"   -> BaseObjects0 arn:aws:s3:::rask-observability/*

So one declared base turns a credential scoped to one table prefix into a credential that can read an
entire bucket — including a bucket the table has nothing to do with, since a base is allowed its own
bucket by design.

WHY THE INPUT IS NOT TRUSTED. The bases are read from the table's own manifest
(`_dataset_facts` -> `manifest_base_path_refs`), and a write-tier vended credential grants `PutObject`
on `<prefix>/*`, which covers `_versions/` — the estate's own vending notes record that this is enough
to commit a whole Lance version client-side. So the value reaching this policy is one a writer on ONE
table can choose, and it is used to widen that writer's own next credential.

THE EXISTING GUARD IS REAL AND IS NOT THIS ONE. `_reject_iam_metacharacters` refuses `*` and `?` in a
base, which stops a wildcard from being smuggled into an ARN — and is exactly why the field looked
checked. It says nothing about WHERE the base points.

WHAT THIS FIXES AND WHAT IT DOES NOT, stated because the difference matters. A base at a bucket ROOT is
refused: no legitimate base is a bucket root — the spec's base path points at a dataset root or a file
directory (`file_format.md`, Base Path System) — so refusing it cannot narrow a real table. A base
naming another tenant's SPECIFIC prefix is not distinguishable here from a legitimate cross-bucket
base, and closing that needs the same read authorization the caller would need to read that table
directly, per base. That residual is tracked, not silently accepted.
"""

from __future__ import annotations

import pytest

from catalog.core.vending import build_session_policy


def _statements(policy: dict[str, object]) -> list[dict[str, object]]:
    """The policy's statements, narrowed — `build_session_policy` returns `dict[str, object]`.

    The `isinstance` is the narrowing AND a real assertion: a policy whose `Statement` is not a list is
    not a policy, and every gate below would otherwise pass vacuously over an empty comprehension.
    """
    statements = policy["Statement"]
    assert isinstance(statements, list), "an STS policy must carry a Statement list"
    return [statement for statement in statements if isinstance(statement, dict)]


def _base_resources(bases: tuple[str, ...]) -> list[str]:
    policy = build_session_policy("lakehouse", "acme-wh/mine$t", "read", bases)
    return [str(s.get("Resource")) for s in _statements(policy) if str(s["Sid"]).startswith("BaseObjects")]


@pytest.mark.parametrize("base", ["s3://lakehouse", "s3://lakehouse/", "s3://rask-observability", "s3://other-bucket//"])
def test_a_base_at_a_bucket_root_is_refused(base: str) -> None:
    """THE GATE. Each of these widens one table's credential to a whole bucket."""
    with pytest.raises(ValueError):
        build_session_policy("lakehouse", "acme-wh/mine$t", "read", (base,))


def test_a_base_that_names_a_real_location_still_grants_it() -> None:
    """The other half: multi-base is a supported layout and a guard that broke it would be worse.

    A base may legitimately live in its own bucket — the policy builds a separate statement pair for
    exactly that reason — so containment here is about DEPTH, not about the bucket matching.
    """
    assert _base_resources(("s3://lakehouse/acme-wh/mine$t/_bases/0",)) == ["arn:aws:s3:::lakehouse/acme-wh/mine$t/_bases/0/*"]
    assert _base_resources(("s3://other-bucket/acme/data",)) == ["arn:aws:s3:::other-bucket/acme/data/*"]


def test_the_table_prefix_itself_is_unaffected() -> None:
    """The vend's own scope must not move — this guard is about the bases appended beside it."""
    policy = build_session_policy("lakehouse", "acme-wh/mine$t", "read", ())
    objects = [s for s in _statements(policy) if s["Sid"] == "TableObjects"]

    assert [str(s["Resource"]) for s in objects] == ["arn:aws:s3:::lakehouse/acme-wh/mine$t/*"]
