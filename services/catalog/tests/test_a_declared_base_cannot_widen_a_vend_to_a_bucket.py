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

THIS FILE OWNS THE BUCKET-ROOT GATE, WHICH IS A REFUSAL. No legitimate base is a bucket root — the
spec's base path points at a dataset root or a file directory (`file_format.md`, Base Path System) — so
raising cannot narrow a real table, and a malformed base is worth an operator's attention.

WHERE a well-formed base points is a separate question with a separate answer, and it is a DROP rather
than a refusal: see `test_a_declared_base_cannot_reach_a_table_the_caller_never_opened.py`. A base is
granted only inside the table's own vended scope or on the operator's `LANCE_MULTIBASE_DATA_BASES`
allowlist, so the assertions here pass no allowlist and expect foreign bases to be absent.
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

    A base inside the table's own vended scope is granted — that is what a registered base, a shallow
    clone and a branch all are. A base in its OWN bucket needs the operator's allowlist, which this
    call deliberately does not pass, so it is absent here rather than granted.
    """
    assert _base_resources(("s3://lakehouse/acme-wh/mine$t/_bases/0",)) == ["arn:aws:s3:::lakehouse/acme-wh/mine$t/_bases/0/*"]
    assert _base_resources(("s3://other-bucket/acme/data",)) == []


def test_the_table_prefix_itself_is_unaffected() -> None:
    """The vend's own scope must not move — this guard is about the bases appended beside it."""
    policy = build_session_policy("lakehouse", "acme-wh/mine$t", "read", ())
    objects = [s for s in _statements(policy) if s["Sid"] == "TableObjects"]

    assert [str(s["Resource"]) for s in objects] == ["arn:aws:s3:::lakehouse/acme-wh/mine$t/*"]
