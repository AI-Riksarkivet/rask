"""Every compaction door in the lakehouse carries a bound, and a new one cannot be added without.

[[LH-185]]. `ds.optimize.compact_files()` is the one call in this estate that moves bytes in
proportion to the DATA rather than to the request, and its Lance defaults are an 8192-ROW read batch
and the HOST's core count as `num_threads`. Against ~1.8 MB bronze rows (measured) that is ~15 GB per
thread in a pod whose limit is 512Mi — incident #93, which OOMKilled the sweep.

The estate learned this three times, once per door: the maintenance sweep (#93), the catalog's
`compact_now` and then its erasure door, which reached `compact_files()` with no bound at all while
the sibling in the same service pinned one and its own comment named the hazard. Three doors is a
pattern, and the fourth is the one this gate exists for.

WHAT IT PROVES AND WHAT IT DOES NOT. It resolves the keywords each call site actually carries,
following ONE hop of indirection — a `**`-unpacked dict built in the same function, updated from a
module constant, or handed in as a parameter by a caller in the same module. That is enough to refuse
a door that names no bound. It cannot prove the bound is the RIGHT size; the per-door behavioural
probes do that (`services/maintenance/tests/test_a_compaction_is_bounded_even_when_nobody_asks.py`,
`tests/unit/test_the_worker_can_hold_every_unit_it_admits.py`).

ONE GATE, NOT ONE PER SERVICE — the same reason `test_no_lakehouse_service_opens_lance_unbounded.py`
gives: a per-service copy is how three half-enforced versions of one invariant drift apart.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]

#: The lakehouse is these four. A compaction door in any of them runs in a pod sized for coordination.
LAKEHOUSE_SERVICES = ("catalog", "lineage", "medallion", "maintenance")

#: The keys that constitute a bound. `max_source_bytes` is required rather than merely preferred: a
#: row count is a ceiling in a unit nobody can size in advance — on a blob tier one row is the blob —
#: so it is weakest exactly where the data is biggest. The two row proxies are required beside it
#: because they bound a different thing (one read chunk, and how many threads hold one each), and the
#: pod's exposure is their product.
REQUIRED_BOUNDS = frozenset({"max_source_bytes", "batch_size", "num_threads"})


def _dict_keys_assigned_to(name: str, scope: Scope, constants: dict[str, frozenset[str]]) -> set[str]:
    """Keys a local dict called `name` is given in `scope` — literal, subscript, or `.update(CONST)`."""
    keys: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            if isinstance(node.value, ast.Dict):
                keys |= {k.value for k in node.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            # A conditional literal (`{...} if x else {}`) contributes the keys it can contribute.
            if isinstance(node.value, ast.IfExp):
                for branch in (node.value.body, node.value.orelse):
                    if isinstance(branch, ast.Dict):
                        keys |= {k.value for k in branch.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == name
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == name
        ):
            for arg in node.args:
                if isinstance(arg, ast.Name):
                    keys |= constants.get(arg.id, frozenset())
    return keys


def _module_constants(tree: ast.Module) -> dict[str, frozenset[str]]:
    """Module-level `NAME = {"k": v}` — the shape `COMPACTION_BOUND` is declared in."""
    out: dict[str, frozenset[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        else:
            continue
        if isinstance(value, ast.Dict):
            out[target] = frozenset(k.value for k in value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str))
    return out


def _resolve_imported_dicts(tree: ast.Module) -> dict[str, frozenset[str]]:
    out: dict[str, frozenset[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and node.module):
            continue
        parts = node.module.split(".")
        for root in (REPO / "services").glob("*/src"):
            candidate = root.joinpath(*parts).with_suffix(".py")
            if not candidate.exists():
                continue
            defined = _module_constants(ast.parse(candidate.read_text(), filename=str(candidate)))
            for alias in node.names:
                if alias.name in defined:
                    out[alias.asname or alias.name] = defined[alias.name]
    return out


#: A scope a `**`-unpacked dict can be built in: the module itself, or the function around the call.
Scope = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef


def _enclosing(tree: ast.Module, node: ast.Call) -> Scope:
    """The function a call sits in — the scope a local `size_kw` is built in."""
    best: Scope = tree
    for candidate in ast.walk(tree):
        if isinstance(candidate, ast.FunctionDef | ast.AsyncFunctionDef):
            end = candidate.end_lineno
            if end is not None and candidate.lineno <= node.lineno <= end:
                best = candidate
    return best


def _keys_at_call(call: ast.Call, tree: ast.Module, constants: dict[str, frozenset[str]]) -> set[str]:
    keys = {kw.arg for kw in call.keywords if kw.arg}
    scope = _enclosing(tree, call)
    for kw in call.keywords:
        if kw.arg is not None or not isinstance(kw.value, ast.Name):
            continue
        name = kw.value.id
        keys |= constants.get(name, frozenset())
        keys |= _dict_keys_assigned_to(name, scope, constants)
        # ONE HOP OUT: the dict may be a PARAMETER, built by a caller in the same module. This is the
        # shape `optimize._rewrite(ds, size_kw, ...)` has, and skipping it would fail the very call
        # site whose bound is the most carefully built one in the estate.
        params = [a.arg for a in scope.args.args] if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef) else []
        if name in params:
            for outer in ast.walk(tree):
                if not (isinstance(outer, ast.Call) and isinstance(outer.func, ast.Name) and outer.func.id == getattr(scope, "name", None)):
                    continue
                for arg in [*outer.args, *(k.value for k in outer.keywords)]:
                    if isinstance(arg, ast.Name):
                        keys |= _dict_keys_assigned_to(arg.id, _enclosing(tree, outer), constants)
    return keys


def _doors(service: str) -> list[tuple[str, set[str]]]:
    """`file:line` and the keys each `compact_files(...)` call carries, for one service."""
    root = REPO / "services" / service / "src"
    found: list[tuple[str, set[str]]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        constants = _module_constants(tree) | _resolve_imported_dicts(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "compact_files":
                found.append((f"{path.relative_to(root)}:{node.lineno}", _keys_at_call(node, tree, constants)))
    return found


@pytest.mark.parametrize("service", LAKEHOUSE_SERVICES)
def test_every_compaction_door_names_a_bound(service: str) -> None:
    """Exhaustive, not a sample: ONE unbounded door is one path that can OOMKill its pod."""
    unbounded = {where: sorted(REQUIRED_BOUNDS - keys) for where, keys in _doors(service) if not keys >= REQUIRED_BOUNDS}
    assert not unbounded, (
        f"{service}: compact_files() is reached without {unbounded} — Lance's defaults are an 8192-ROW "
        f"batch, the HOST's core count and no byte ceiling at all, which is how incident #93 OOMKilled "
        f"the sweep. Every door passes all of {sorted(REQUIRED_BOUNDS)}"
    )


def test_the_gate_can_see_the_doors_it_is_guarding() -> None:
    """A resolver that found nothing would pass every service silently — the failure mode this
    estate has shipped before (a gate reading an empty argv list and approving anything)."""
    total = sum(len(_doors(s)) for s in LAKEHOUSE_SERVICES)
    assert total >= 4, f"only {total} compact_files call sites found; the lakehouse has at least four, so the walk is broken"
