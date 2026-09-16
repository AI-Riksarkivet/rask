"""A multi-base table is refused a direct credential only for bases the session policy cannot cover.

[[LH-057]]. The union vend landed — `build_session_policy` emits a `ListBase<n>`/`BaseObjects<n>` pair
per SANCTIONED base — but the door in front of it still short-circuits on the old premise: if the
multi-base feature is on and ANY fragment carries a `base_id`, vend `server_mediated` and stop. That
premise was true when the policy was scoped to the primary bucket alone; it is not true now, and the
result is that the estate's own multi-base tables are proxied through the catalog's ROOT credential
rather than direct-vended with a policy that reaches exactly their bases.

THE GUARD IS KEPT, NARROWED, NOT DROPPED. `_base_is_sanctioned` is what decides whether the policy may
grant a base at all — inside the table's own vended scope, or on the operator's allowlist — so a base
that fails it genuinely cannot be reached by a direct client, and falling back is right for that table.
What was wrong was answering the question for every base at once.

THE DECLARED LIST, NOT THE FRAGMENT SCAN, and it is the safe direction: the manifest's `base_paths` are
a SUPERSET of the bases fragments actually resolve through, so "every declared base is sanctioned"
implies every fragment is covered. The old check walked every fragment's data files to find one
`base_id`; this reads the manifest the vend door already opens for the version.

BOTH DOORS OR NEITHER. `has_external_bases`'s own docstring says the vend door and describe-with-vending
"must answer it identically" — and measured 2026-09-16 they did not even ask the same question:
`tables.py:418` vends with no `bases=` at all, so a describe-vended credential is scoped to less than
the table is on every multi-base table that passes the guard.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from catalog.core.vending import unsanctioned_bases


TABLE = "s3://acme-wh/9f3_acme$t"


def test_a_base_inside_the_tables_own_scope_needs_no_sanction() -> None:
    """A shallow clone and a branch are this shape, and measured 2026-09-13 it is what live tables
    declare — the credential already covers it."""
    missed = unsanctioned_bases(TABLE, [f"{TABLE}/tree/work"], sanctioned_bases=())

    assert missed == (), f"a base under the table's own prefix was treated as unreachable: {missed}"


def test_a_foreign_base_on_the_operator_allowlist_is_covered() -> None:
    missed = unsanctioned_bases(TABLE, ["s3://lance-catalog/models/resnet"], sanctioned_bases=["s3://lance-catalog/models/"])

    assert missed == (), f"an allowlisted base was treated as unreachable: {missed}"


def test_a_foreign_base_nobody_sanctioned_is_what_forces_the_fallback() -> None:
    missed = unsanctioned_bases(TABLE, [f"{TABLE}/tree/work", "s3://someone-elses-bucket/data"], sanctioned_bases=["s3://lance-catalog/models/"])

    assert missed == ("s3://someone-elses-bucket/data",), (
        f"the unsanctioned base was not singled out ({missed}) — the door would either proxy a table it "
        "could have vended directly, or direct-vend one whose bytes the policy cannot reach"
    )


def test_a_table_with_no_declared_bases_never_falls_back() -> None:
    """The overwhelmingly common case, and the one the old feature flag existed to keep cheap."""
    assert unsanctioned_bases(TABLE, [], sanctioned_bases=()) == ()


def _source(module: str) -> str:
    return (Path(__file__).resolve().parents[1] / "src" / "catalog" / "api" / "v1" / "endpoints" / f"{module}.py").read_text(encoding="utf-8")


def test_both_vending_doors_ask_the_same_question() -> None:
    """Read off the source, because the two doors are in different modules and only a reader ever
    compared them. The fragment scan must be gone from both: it answers the pre-union premise."""
    for module in ("credentials", "tables"):
        body = _source(module)

        assert "unsanctioned_bases(" in body, f"{module}.py does not narrow the multi-base fallback to the bases the policy misses"
        assert "has_external_bases" not in body, (
            f"{module}.py still calls `has_external_bases`, which answers 'does any fragment live in a base' — "
            "the question that mattered before the policy could grant bases, and the one that proxies a table it could vend"
        )


def test_describe_with_vending_scopes_the_credential_to_the_TABLE_not_less() -> None:
    """`tables.py` vended with no `bases=`, so its credential could not reach the very bases the other
    door's policy grants — the § H12 shortfall, on the door nobody re-checked."""
    body = _source("tables")
    call = body[body.index("vendor.vend(") :][:400]

    assert "bases=" in call, f"describe-with-vending still vends without the table's declared bases: {call.splitlines()[0]!r}"


def test_the_shared_manifest_read_lives_where_both_doors_can_reach_it() -> None:
    """One read, one spelling. A private copy in each endpoint module is how the two answers drifted."""
    from catalog.core import vending

    assert hasattr(vending, "dataset_facts"), "the manifest read is still private to one endpoint module"
    assert "base_paths" in (inspect.getdoc(vending.dataset_facts) or "").lower() or "base" in (inspect.getdoc(vending.dataset_facts) or "").lower()
