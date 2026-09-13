"""A manifest-declared base is granted READ, and nothing confined that grant to the caller's tenancy.

`build_session_policy` appends a READ statement for every base the table's manifest declares. The two
guards already on that loop check the base's SHAPE — `_reject_iam_metacharacters` refuses `*`/`?`,
`_reject_a_base_that_is_not_a_location` refuses a bucket root and a `..` segment — and neither says
anything about WHERE a well-formed base points.

WHY THE INPUT IS NOT TRUSTED. The bases come off the table's own manifest (`_dataset_facts` ->
`manifest_base_path_refs`), and a write-tier vend grants `PutObject` on `<prefix>/*`, which covers
`_versions/` — enough to commit a Lance version client-side. So a writer on ONE table can declare a base
naming another tenant's prefix and read it with their next credential.

THE SANCTION ALREADY EXISTS AND THE VEND DOOR IGNORED IT. `LANCE_MULTIBASE_DATA_BASES` is the operator's
allowlist of legitimate foreign bases, and its own config note states the rule: "A per-request
`data_base` MUST be on this list — a caller can never point a base at an arbitrary bucket (data-exfil /
rogue-write door)." The CREATE door enforces it; the vend door did not. This is that asymmetry closed,
not a new mechanism.

So a base is granted when it is inside the table's own vended scope — which is what a shallow clone or a
branch is, and measured 2026-09-13 is the shape live tables actually declare (`<table-root>/tree/work`)
— or when the operator has sanctioned it. Anything else is dropped.

DROPPED, NEVER RAISED, and that is the difference from the two sibling guards on the same loop: a
poisoned manifest must not make a table permanently un-vendable. The caller keeps the credential it was
entitled to and loses only the grant it was not.
"""

from __future__ import annotations

import pytest

from catalog.core.vending import build_session_policy


BUCKET = "lakehouse"
PREFIX = "acme-wh/mine$t"
SANCTIONED = ("s3://data-bases/acme",)


def _statements(policy: dict[str, object]) -> list[dict[str, object]]:
    statements = policy["Statement"]
    assert isinstance(statements, list), "an STS policy must carry a Statement list"
    return [statement for statement in statements if isinstance(statement, dict)]


def _base_resources(*bases: str, sanctioned: tuple[str, ...] = SANCTIONED) -> list[str]:
    policy = build_session_policy(BUCKET, PREFIX, "read", bases, sanctioned_bases=sanctioned)
    return [str(s.get("Resource")) for s in _statements(policy) if str(s["Sid"]).startswith("BaseObjects")]


@pytest.mark.parametrize(
    "base",
    [
        pytest.param(f"s3://{BUCKET}/{PREFIX}/tree/work", id="branch-under-the-table-root"),
        pytest.param(f"s3://{BUCKET}/{PREFIX}/_bases/0", id="registered-base-under-the-table-root"),
        pytest.param(f"s3://{BUCKET}/{PREFIX}", id="the-table-root-itself"),
    ],
)
def test_a_base_inside_the_tables_own_scope_is_still_granted(base: str) -> None:
    """The live shape. A shallow clone or a branch sits under the table's own root, and the credential
    already reaches it — refusing these would break the layout the base grant exists to serve."""
    assert _base_resources(base) == [f"arn:aws:s3:::{base.removeprefix('s3://')}/*"]


@pytest.mark.parametrize(
    "base",
    [
        pytest.param(f"s3://{BUCKET}/acme-wh/theirs$t", id="another-table-in-the-same-bucket"),
        pytest.param(f"s3://{BUCKET}/{PREFIX}-evil/data", id="sibling-prefix-near-miss"),
        pytest.param(f"s3://{BUCKET}-evil/{PREFIX}", id="sibling-bucket-near-miss"),
        pytest.param("s3://other-bucket/acme/data", id="an-unsanctioned-foreign-bucket"),
        pytest.param("s3://data-bases-evil/acme", id="near-miss-on-a-sanctioned-base"),
        pytest.param("s3://data-bases/acme-evil", id="near-miss-on-a-sanctioned-prefix"),
    ],
)
def test_a_base_outside_the_scope_and_the_sanction_is_dropped(base: str) -> None:
    """THE GATE. Each of these is a writer-chosen path reaching bytes the vend never authorized.

    The two near-misses are the reason containment is checked against `<base>/` rather than as a bare
    prefix test: `s3://lakehouse-evil/...` starts with `s3://lakehouse`.
    """
    assert _base_resources(base) == []


def test_a_base_the_operator_sanctioned_is_granted() -> None:
    """The other half: multi-base distribution is a supported layout and a guard that broke it would be
    worse than the hole. The allowlist is how an operator says a foreign bucket is legitimate."""
    assert _base_resources("s3://data-bases/acme") == ["arn:aws:s3:::data-bases/acme/*"]
    assert _base_resources("s3://data-bases/acme/part-0") == ["arn:aws:s3:::data-bases/acme/part-0/*"]


def test_nothing_is_sanctioned_by_default() -> None:
    """FAIL CLOSED. A caller that never wires the allowlist grants no foreign base — the empty default
    must mean "nothing foreign is sanctioned", never "sanctioning is off so allow everything"."""
    assert _base_resources("s3://data-bases/acme", sanctioned=()) == []


def test_a_dropped_base_does_not_cost_the_caller_the_credential() -> None:
    """A poisoned manifest must not make a table un-vendable: the vend still renders its own scope."""
    policy = build_session_policy(BUCKET, PREFIX, "read", ("s3://other-bucket/acme/data",), sanctioned_bases=SANCTIONED)
    sids = [str(s["Sid"]) for s in _statements(policy)]
    assert sids == ["ListTablePrefix", "TableObjects"], f"the table's own statements must survive, got {sids}"


def test_the_dropped_base_does_not_renumber_the_ones_that_survive() -> None:
    """Sids are positional. Dropping the first of two must not leave a `BaseObjects1` with no
    `BaseObjects0`, which would read as a missing statement to anyone diffing a rendered policy."""
    resources = _base_resources("s3://other-bucket/nope", "s3://data-bases/acme")
    assert resources == ["arn:aws:s3:::data-bases/acme/*"]
    policy = build_session_policy(BUCKET, PREFIX, "read", ("s3://other-bucket/nope", "s3://data-bases/acme"), sanctioned_bases=SANCTIONED)
    base_sids = sorted(str(s["Sid"]) for s in _statements(policy) if "Base" in str(s["Sid"]))
    assert base_sids == ["BaseObjects0", "ListBase0"], f"surviving bases must be numbered from zero, got {base_sids}"


@pytest.mark.parametrize("base", ["s3://lakehouse", "s3://other-bucket//"])
def test_a_bucket_root_is_still_refused_rather_than_dropped(base: str) -> None:
    """The sibling guard keeps its teeth: a bucket root is malformed, not merely unsanctioned, and the
    difference is worth an operator's attention rather than a silent drop."""
    with pytest.raises(ValueError):
        build_session_policy(BUCKET, PREFIX, "read", (base,), sanctioned_bases=SANCTIONED)


def test_a_wildcard_base_is_still_refused() -> None:
    with pytest.raises(ValueError):
        build_session_policy(BUCKET, PREFIX, "read", ("s3://data-bases/acme*",), sanctioned_bases=SANCTIONED)
