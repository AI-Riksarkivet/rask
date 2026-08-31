"""A door that ACCEPTS `branch` must not hand it to an implementation that ignores it.

WHY THIS IS A GATE AND NOT A LIST OF FIXES. Nine doors shipped with this defect and were found one at
a time by driving the live catalog: `update`, `delete`, `insert`, `merge_insert`, the merge's index
build, `schema_metadata/update`, `count_rows`, `query`, `explain_plan`, `analyze_plan`. Every one had
the same shape — the route declares `branch`, fills it into the spec request, and passes the whole
request to `native.call`, whose upstream `dir` implementation disregards it. The request is then
answered for MAIN with a 200. Nothing errors and nothing logs; a branch-scoped write lands in the
dataset the branch existed to protect, and a branch-scoped read returns a plausible number for a
question nobody asked.

Twenty-seven of the fifty-three spec request models carry `branch`, so this is not a defect that was
found and finished — it is a shape the codebase can reproduce every time someone wires a new door the
obvious way. The gate is the shape, not the ten instances.

WHAT COUNTS AS HANDLING IT, and each of these is a real strategy in the tree rather than a loophole:

* Serving the branch — the function opens the ref itself (`open_dataset(..., branch=...)`), which is
  also where spec error 22 `TableBranchNotFound` comes from for a ref that does not exist.
* Refusing it — `refuse_a_branch_this_door_cannot_honour`, a 501 `Unsupported`. Correct where a
  faithful implementation would mean re-deriving a large surface (vector search, full-text search,
  prefilter, nprobes, refine_factor, distance_type) and a subtle divergence would be a wrong answer
  wearing the right shape.
* Never accepting it — the function constructs the request WITHOUT a branch and no caller can supply
  one. Detected structurally: the model is built inline with no `branch=` keyword and no `branch` name
  is read anywhere in the function.

An entry in `_ANSWERED` is the fourth option and it must carry a MEASUREMENT a reader can check. It is
keyed by `module::function`, never by line number: a two-line edit above the call would silently
retire a keyed-by-line exemption, which is the failure mode an exemption list exists to prevent.

WHAT THIS GATE IS AND IS NOT. It does not know which upstream operations honour `branch` — nothing
static can, because the answer lives in Rust behind `self._inner` and differs per operation. Driven on
2026-08-31, `describe_table_version` and `batch_delete_table_versions` honour it while
`list_table_versions` did not, from adjacent lines of the same module. So this gate does not assert a
bug; it asserts that somebody LOOKED. A new door that hands a branch to `native.call` fails here until
its author drives it and records what happened, which is the step that was skipped ten times.
"""

from __future__ import annotations

import ast
import pathlib

import lance_namespace as ln


_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog"

#: The spec request models whose `branch` the upstream `dir` implementation does not honour.
_BRANCHED_MODELS = frozenset(name for name in dir(ln) if name.endswith("Request") and "branch" in getattr(ln, name).model_fields)

#: Names that prove the function decided what to do with the branch it was handed.
_HANDLED = ("refuse_a_branch_this_door_cannot_honour", "open_dataset")

#: `module::function` -> why this site needs no branch handling. A MEASUREMENT, not a silencer.
#:
#: THE UPSTREAM IS NOT UNIFORMLY WRONG, and this dict is where that is recorded. The first cut of this
#: gate assumed every `native.call` drops `branch` and flagged five doors; driving them showed only one
#: was broken. An assumption that is right a fifth of the time is not a gate, it is noise that trains
#: readers to add exemptions without looking. So the rule is: every entry names what was DRIVEN and
#: what came back. A reason with no observation in it is not an answer to this gate.
_ANSWERED: dict[str, str] = {
    "api/v1/endpoints/versions.py::describe_table_version": (
        "HONOURS it. Driven 2026-08-31 on a table whose branch had diverged to version 4 while main sat "
        "at 1: `branch=work` returned version 4 with manifest_path under `tree/work/_versions/`, and a "
        "branch that had never been created returned 404."
    ),
    "api/v1/endpoints/versions.py::batch_delete_table_versions": (
        "HONOURS it. Driven 2026-08-31: `branch=work` with ranges [2,3] returned deleted_count 0 and "
        "left every main version readable; `branch=ghost-never-made` was refused 404 'branch not "
        "found'. A door that 404s an absent ref is a door that read the parameter."
    ),
    "api/v1/endpoints/columns.py::backfill_column": (
        "MOOT. Driven 2026-08-31: the dir backend answers `alter_table_backfill_columns` with 501 "
        "'Not supported' for every request, branch or no branch, so there is no target to get wrong. "
        "Revisit if a backend ever implements the op."
    ),
    "api/v1/endpoints/versions.py::list_table_versions": (
        "HONOURS it. Driven 2026-08-31 on a table whose branch had diverged to version 4 while main sat "
        "at 1: `?branch=work` returned 4 versions with manifest paths under `tree/work/_versions/`, and "
        "`?branch=ghost-never-made` returned 404. NOTE THE CHANNEL — `branch` is a QUERY parameter on "
        "this route, not a body field. A first probe sent it in the JSON body, FastAPI ignored it, the "
        "door answered for main, and that looked exactly like the defect. It was not one."
    ),
    "api/v1/endpoints/versions.py::create_table_version": (
        "UNDRIVEN, and recorded as such rather than assumed safe. Driving 2026-08-31 got 422 on the "
        "request shape before reaching any branch behaviour, so nothing is known about it. This entry "
        "is a debt, not a clearance — it is in `open_backlog.md` under the branch sweep."
    ),
}


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _calls_native(fn: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "call"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "native"
        for n in ast.walk(fn)
    )


def _branched_models_in(fn: ast.AST) -> tuple[set[str], set[str]]:
    """The branch-carrying models this function takes as a BODY, and the ones it CONSTRUCTS.

    The distinction decides whether a branch can reach the call at all, and getting it wrong makes the
    gate miss the worst case. A parameter annotated `body: UpdateTableRequest` is parsed by FastAPI
    from the request body, so the CLIENT supplies `branch` and the handler never has to name it — the
    first cut of this gate looked for the identifier `branch` in the function and therefore skipped
    every body-typed door, which is precisely the shape `query_table` had. A model the handler builds
    itself can only carry a branch the handler puts there.
    """
    from_body: set[str] = set()
    constructed: set[str] = set()
    for arg in list(fn.args.args) + list(fn.args.kwonlyargs):
        annotation = arg.annotation
        if isinstance(annotation, ast.Name) and annotation.id in _BRANCHED_MODELS:
            from_body.add(annotation.id)
        elif isinstance(annotation, ast.BinOp) and isinstance(annotation.left, ast.Name) and annotation.left.id in _BRANCHED_MODELS:
            from_body.add(annotation.left.id)  # `Model | None` — still parsed from the body
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _BRANCHED_MODELS:
            constructed.add(node.func.id)
    return from_body, constructed


def _mentions_branch(fn: ast.AST) -> bool:
    """Does a `branch` value reach this function at all?

    A handler that builds its request with no `branch=` and never names `branch` cannot drop one — the
    caller has no channel to supply it. That is a genuine third strategy, not an oversight, and
    treating it as an offence would force meaningless guards onto doors that are already safe.
    """
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == "branch":
            return True
        if isinstance(node, ast.keyword) and node.arg == "branch":
            return True
        if isinstance(node, ast.arg) and node.arg == "branch":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "branch":
            return True
    return False


def test_the_walk_sees_the_catalog_and_the_branched_models() -> None:
    """A gate that inspects nothing passes everything."""
    assert len(_modules()) > 40, f"only {len(_modules())} modules — the walk is not seeing the catalog source"
    assert len(_BRANCHED_MODELS) > 20, f"only {len(_BRANCHED_MODELS)} branch-carrying models — the spec introspection is broken, so every site would look safe"


def test_no_door_hands_a_branch_to_an_implementation_that_ignores_it() -> None:
    offences: list[str] = []
    for path in _modules():
        tree = ast.parse(path.read_text())
        for fn in _functions(tree):
            key = f"{path.relative_to(_SRC)}::{fn.name}"
            if key in _ANSWERED or not _calls_native(fn):
                continue
            from_body, constructed = _branched_models_in(fn)
            # A body-typed model ALWAYS carries a client-supplied branch; a constructed one only if
            # this function puts one there.
            models = from_body | (constructed if _mentions_branch(fn) else set())
            if not models:
                continue
            source = ast.get_source_segment(path.read_text(), fn) or ""
            if any(marker in source for marker in _HANDLED):
                continue
            offences.append(
                f"{key} (line {fn.lineno}) passes {', '.join(sorted(models))} to native.call with a branch in scope and neither serves nor refuses it"
            )

    assert not offences, (
        "a door accepts `branch` and hands it to an implementation that disregards it — the request will "
        "be answered for MAIN with a 200, which is how nine of these shipped:\n  " + "\n  ".join(offences) + "\n\n"
        "Serve it (`open_dataset(..., branch=...)`), refuse it "
        "(`refuse_a_branch_this_door_cannot_honour`), or add a `module::function` entry to `_ANSWERED` "
        "with a reason a reader can check."
    )
