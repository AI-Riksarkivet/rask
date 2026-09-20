"""A catalog door that destroys a governed object must also revoke that object's FGA tuples.

Found the hard way on 2026-09-20. `DELETE /v1/warehouses/{id}?cascade=true` dropped each bound
namespace through the NATIVE `drop_namespace` — which destroys its child tables inside one call —
and then revoked `namespace:<id>` and nothing else. Every table it destroyed kept its authorization
tuples while its dataset was gone, and the live count was **1,033**.

NOTHING WOULD HAVE CAUGHT IT. The tuples are invisible to `revoke_object_tuples`, which reads BY
OBJECT; they surfaced only as a side effect on a LINEAGE metric (`unknown_to_graph`, "governed minus
graph") under an alert whose words are "a write lost its provenance". A hand sweep of the five
destructive paths is what found it, and a hand sweep is not repeatable.

SOURCE-LEVEL, DELIBERATELY. The alternative is driving each door against a real namespace plus a
real OpenFGA, which is an integration concern and would not run in this tier at all. What this asks
is narrow and checkable: a function that calls a destroying native op names a revoke in the same
body. That cannot prove the revoke is COMPLETE — only that the door knows it owes one — and the
completeness of any single door is its own test's job (`test_a_warehouse_cascade_revokes_its_tables`
pins the one this rule was written for).

A door may opt out by naming its reason in `_NO_REVOKE_NEEDED`, and two already do: the reason is
what makes the exemption reviewable rather than a hole.
"""

from __future__ import annotations

import ast
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[2]
ENDPOINTS = ROOT / "services/catalog/src/catalog/api/v1/endpoints"

#: Native operations that DESTROY a governed object. `drop_*` on a tier that carries FGA tuples.
_DESTROYS = ("drop_table", "drop_namespace", "deregister_table")
#: A CALL whose name revokes. Matched as a called name rather than as text, because a substring test
#: is not a test: `_revoke` appears inside `tuples_revoked`, a RESPONSE FIELD, so the first version of
#: this gate stayed green against a `delete_warehouse` with every revoke stripped out of it. Mutation
#: checking is what said so.
_REVOKES = frozenset({"revoke_ownership", "revoke_object_tuples", "_revoke_tuples", "_revoke_descendants_of", "_revoke"})


def _called_names(node: ast.AST) -> set[str]:
    """Every function name CALLED inside ``node`` — bare, attribute and awaited alike."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


#: A DOOR, by this package's naming convention. The narrowing is load-bearing and was measured: the
#: unfiltered rule flagged nine functions and seven were false — `create_namespace`, `declare_table`
#: and `register_table` name a drop op for ROLLBACK, `_undo_*` are those rollbacks, and
#: `_destroy_subtree` / `_trash_subtree` are helpers whose CALLERS revoke. A door that destroys as its
#: purpose is spelled drop/delete/deregister/purge and is public; anything else is compensation or a
#: fragment, and asking either to revoke would be asking the wrong layer.
_DOOR = ("drop_", "delete_", "deregister_", "purge_")

#: value = why this door destroys without revoking. Empty today, and an entry needs a reason rather
#: than a name — that is what makes an exemption reviewable instead of a hole.
_NO_REVOKE_NEEDED: dict[str, str] = {}


def _functions_that_destroy() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for path in sorted(ENDPOINTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            body = ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
            if not node.name.startswith(_DOOR):
                continue
            if any(f'"{op}"' in body for op in _DESTROYS):
                found[node.name] = node
    return found


def test_every_destroying_door_also_revokes() -> None:
    destroyers = _functions_that_destroy()
    offenders = []
    for name, node in sorted(destroyers.items()):
        if name in _NO_REVOKE_NEEDED:
            continue
        if not (_called_names(node) & _REVOKES):
            offenders.append(name)

    assert not offenders, (
        "these doors destroy a governed object and never revoke its tuples, so the tuples outlive the "
        "object and are invisible to every read-by-object cleanup — name the revoke, or record the "
        f"exemption in `_NO_REVOKE_NEEDED` with its reason: {offenders}"
    )


def test_the_walk_actually_finds_the_destroying_doors() -> None:
    """Without this a renamed op or a moved package would make the rule above pass over nothing."""
    found = set(_functions_that_destroy())

    assert {"drop_table", "drop_namespace", "delete_warehouse"} <= found, f"the walk missed known destroyers: {sorted(found)}"


def test_no_exemption_outlives_the_door_it_excuses() -> None:
    """An exemption for a function nobody has any more is a stale licence, not a decision."""
    stale = sorted(set(_NO_REVOKE_NEEDED) - set(_functions_that_destroy()))

    assert not stale, f"these exemptions name doors that no longer destroy anything: {stale}"
