"""Siblings in one file must agree — a fix that lands on one door has to land on its twin.

THE DEFECT CLASS, measured three times on 2026-08-31 inside `services/catalog`, each time as one
member of a set getting the fix and its neighbours not:

* `list_tables` filtered its items against FGA; `list_namespaces`, one route away and gated by the
  same child-widened relation, answered with every sibling namespace NAME.
* `add_columns` / `alter_columns` / `drop_columns` passed `branch=req.branch` to `open_dataset`;
  `update_table` and `delete_from_table` did not, so a branch-scoped write silently hit main and
  returned 200.
* `publish` called `refuse_a_tier_without_provenance`; `gate` — the question a caller asks BEFORE
  the act — did not, so the gate approved what the act then refused.

None of the estate's 7,405 tests saw any of them, because each door does exactly what its own test
says it does. What was wrong was the DISAGREEMENT between doors, and nothing was looking at pairs.

WHY THE POPULATIONS ARE DERIVED FROM THE TREE, never listed here: a roster stops covering the route
somebody adds next week, which is the same failure shape as the bugs above — a set that grew a member
nobody applied the rule to. So the branch population comes from `lance_namespace`'s own request models
(does the model carry a `branch` field?), the listing population from every `list_*` route the
endpoints package defines, the "does the route's gate imply its contents?" question from `model.fga`'s
own `from child` clauses, and the refusal-parity population from every function that runs
`assert_quality`.

Allowlisting is allowed where a divergence is deliberate, and every entry carries its reason inline.
A stale exemption is how a gate rots, so an allowlisted name that no longer exists — or that no longer
meets the condition its reason claims — fails the suite rather than being quietly carried.
"""

from __future__ import annotations

import ast
import pathlib
import re

import lance_namespace
import pytest

from catalog.api.fga_deps import _FGA_TYPE, _RESOURCES, _action_relation


REPO = pathlib.Path(__file__).resolve().parents[2]
CATALOG_SRC = REPO / "services" / "catalog" / "src" / "catalog"
ENDPOINTS = CATALOG_SRC / "api" / "v1" / "endpoints"
MODEL_FGA = REPO / "packages" / "service-kit" / "src" / "service_kit" / "governed" / "auth" / "model.fga"

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})

#: `/v1/<resource>/{<param>}/<suffix>` — the shape `catalog.api.fga_deps.authorize` reads a route's
#: implicit gate off. The param NAME varies by module (`id`, `warehouse_id`, `project_id`), so it is
#: matched rather than assumed.
ROUTE_WITH_ID = re.compile(r"^/v1/(?P<resource>[a-z_-]+)/\{[^}]+\}/(?P<suffix>.+)$")

Function = ast.FunctionDef | ast.AsyncFunctionDef


# --------------------------------------------------------------------------- #
# Tree walking
# --------------------------------------------------------------------------- #


def _modules(root: pathlib.Path) -> list[tuple[pathlib.Path, ast.Module]]:
    return [(path, ast.parse(path.read_text())) for path in sorted(root.rglob("*.py"))]


def _functions(tree: ast.Module) -> list[Function]:
    return [node for node in ast.walk(tree) if isinstance(node, Function)]


def _name(path: pathlib.Path, fn: Function) -> str:
    return f"{path.relative_to(CATALOG_SRC).as_posix()}::{fn.name}"


def _params(fn: Function) -> list[ast.arg]:
    args = fn.args
    return [*args.posonlyargs, *args.args, *args.kwonlyargs]


def _calls(fn: Function) -> list[ast.Call]:
    return [node for node in ast.walk(fn) if isinstance(node, ast.Call)]


def _callee(call: ast.Call) -> str:
    """The bare callee name — `x.y.foo(...)` and `foo(...)` both read as ``foo``."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _const_str(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


# --------------------------------------------------------------------------- #
# (a) branch parity — a request that carries `branch` must reach `open_dataset`
# --------------------------------------------------------------------------- #

#: Request models that DECLARE a `branch` field, read off `lance_namespace` itself. The spec is the
#: authority on which operations are branch-scoped; a local list would be a second opinion that drifts
#: the moment the spec adds one (0.11.0 added `AlterTableBackfillColumnsRequest`, for instance).
BRANCH_CARRYING_REQUESTS = frozenset(
    name for name in dir(lance_namespace) if name.endswith("Request") and "branch" in getattr(getattr(lance_namespace, name), "model_fields", {})
)

#: `name -> why the divergence is correct`. Each entry is checked for staleness by
#: `test_the_branch_allowlist_carries_no_stale_exemption` below.
BRANCH_EXEMPT: dict[str, str] = {
    "services/dataplane.py::create_tag": (
        "The branch names the REF the tag points at, not the dataset to open: pylance's tag store is "
        "table-wide, so the branch travels through `_tag_reference(req.branch, req.version)` into the "
        "`(branch, version)` reference. Opening the branch instead would put the tag on the branch's own "
        "handle and lose the main-scoped tag namespace the spec's tag ops read back."
    ),
    "services/dataplane.py::update_tag": (
        "Same as `create_tag`: `_tag_reference(req.branch, req.version)` carries the branch into the tag "
        "reference, and the tag itself lives on the table rather than on one branch."
    ),
}


def _branch_sources(fn: Function) -> list[str]:
    """The parameters that hand this function a branch — a plain `branch`, or a branch-carrying request."""
    found = []
    for param in _params(fn):
        if param.arg == "branch":
            found.append(param.arg)
            continue
        if param.annotation is None:
            continue
        annotation = ast.unparse(param.annotation)
        if any(request in annotation for request in BRANCH_CARRYING_REQUESTS):
            found.append(param.arg)
    return found


def _open_dataset_calls(fn: Function) -> list[ast.Call]:
    return [call for call in _calls(fn) if _callee(call) == "open_dataset"]


def _branch_population() -> list[tuple[str, Function, list[str], list[ast.Call]]]:
    """Every catalog function that is HANDED a branch and opens a dataset."""
    population = []
    for path, tree in _modules(CATALOG_SRC):
        for fn in _functions(tree):
            sources = _branch_sources(fn)
            opens = _open_dataset_calls(fn)
            if sources and opens:
                population.append((_name(path, fn), fn, sources, opens))
    return population


def test_a_route_with_an_UNTYPED_body_still_reads_its_branch() -> None:
    """A handler taking `body: dict` must read `branch` out of the envelope itself.

    THE GAP THIS CLOSES IS IN THE SIBLING TEST BELOW, not in the estate. That walk finds branch-carrying
    handlers by their annotated request MODEL — so a route taking `body: dict[str, Any]` is invisible to
    it, and `update_table_schema_metadata` dropped `branch` on both its paths while the walk reported
    clean.

    The spec is explicit about where `branch` lives, and it is not always a parameter: the component's
    own description says the query form is "used by branch-scoped operations that cannot carry a
    `branch` field in their request body (Arrow IPC stream and bodyless operations). Operations with a
    JSON request body carry `branch` as a body field instead."

    So a handler that hand-parses its body owes the envelope the same read it already gives `id` — it
    calls `reconcile_body_id` for one and must not silently ignore the other.
    """
    offenders: list[str] = []
    for path, tree in _modules(ENDPOINTS):
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            untyped_body = any(a.arg == "body" and a.annotation is not None and ast.unparse(a.annotation).startswith("dict") for a in fn.args.args)
            if not untyped_body:
                continue
            source = ast.unparse(fn)
            # It reads the envelope's id, so it is the hand-parsing shape — and `branch` must be read
            # too, or accepted as an explicit parameter.
            if "reconcile_body_id" not in source:
                continue
            takes_branch = any(a.arg == "branch" for a in fn.args.args + fn.args.kwonlyargs)
            if not takes_branch and '"branch"' not in source and "'branch'" not in source:
                offenders.append(f"{path.name}::{fn.name}")

    assert not offenders, (
        "these routes hand-parse a JSON body and reconcile its `id`, but never read its `branch` — so a "
        "branch-scoped call silently acts on MAIN:\n  " + "\n  ".join(sorted(offenders))
    )


#: Source markers proving a door DECIDED about the branch instead of dropping it.
#:
#: Refusing is a legitimate answer, and the estate uses two shapes of it: the shared 406
#: (`refuse_a_branch_this_door_cannot_honour`, for doors whose option surface is too large to serve
#: faithfully — `query`, the index builders, `stats`) and `describe`'s own 400, which names the branch
#: operations instead. A door that refuses cannot write to main under a branch's name, which is the
#: only thing this gate exists to catch — so treating it as a violation would push an author toward
#: passing `branch=` into an open that is unreachable, i.e. toward prose that lies about the door.
#: `test_a_declared_branch_is_never_silently_dropped.py` makes the same allowance for the same reason.
_BRANCH_DECIDED = ("refuse_a_branch_this_door_cannot_honour", "`branch` is not supported by")


def _decides_about_the_branch(fn: Function) -> bool:
    """Does this function REFUSE the branch rather than drop it?

    Read off the AST rather than the source text, so an unrelated mention of the marker in a docstring
    or a comment cannot excuse a door that in fact drops the branch — a source-substring check would
    have been satisfied by this very module's own prose.
    """
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            called = node.func
            if isinstance(called, ast.Attribute) and called.attr == _BRANCH_DECIDED[0]:
                return True
            if isinstance(called, ast.Name) and called.id == _BRANCH_DECIDED[0]:
                return True
        if isinstance(node, ast.Raise):
            for message in ast.walk(node):
                if isinstance(message, ast.Constant) and isinstance(message.value, str) and _BRANCH_DECIDED[1] in message.value:
                    return True
    return False


def test_a_branch_carrying_request_reaches_open_dataset() -> None:
    """A door handed a branch must open the branch, or refuse it — the silent wrong-target write class."""
    population = _branch_population()
    assert population, "no branch-carrying dataset opens found — the walk is looking in the wrong place"

    violations = []
    for name, fn, sources, opens in population:
        if name in BRANCH_EXEMPT:
            continue
        if _decides_about_the_branch(fn):
            continue
        for call in opens:
            if not any(kw.arg == "branch" for kw in call.keywords):
                violations.append(f"{name} (line {call.lineno}) takes {sources} but opens the dataset without `branch=` — the write lands on main")

    assert not violations, "branch-scoped doors that silently write to main:\n  " + "\n  ".join(violations)


def test_the_branch_allowlist_carries_no_stale_exemption() -> None:
    """An exemption must still name a live function that still USES the branch it is excused from forwarding."""
    live = {name: fn for name, fn, _sources, _opens in _branch_population()}
    stale = []
    for name, reason in BRANCH_EXEMPT.items():
        assert reason.strip(), f"{name} is exempt with no reason"
        fn = live.get(name)
        if fn is None:
            stale.append(f"{name} is exempt but no such branch-carrying dataset open exists any more")
            continue
        if not any(isinstance(node, ast.Attribute) and node.attr == "branch" for node in ast.walk(fn)):
            stale.append(f"{name} is exempt because it routes the branch elsewhere, but it no longer reads `.branch` at all")

    assert not stale, "stale branch exemptions:\n  " + "\n  ".join(stale)


# --------------------------------------------------------------------------- #
# (b) listing parity — a listing gated by a relation that does not imply its contents must filter
# --------------------------------------------------------------------------- #

#: `name -> why this listing needs no per-item filter`. Checked for staleness below.
LISTING_EXEMPT: dict[str, str] = {}


def _child_widened_relations() -> frozenset[tuple[str, str]]:
    """`(type, relation)` pairs the model defines with UPWARD visibility (`... from child`).

    These are the relations that do NOT imply their contents: `can_get_metadata` on a container reads
    `reader or can_get_metadata from child`, so holding `reader` on ONE deep leaf opens the route on
    every ancestor. A route gated only on such a relation has been told "this caller may see that
    something under here exists" and answers with the names of everything under there.
    """
    widened: set[tuple[str, str]] = set()
    current: str | None = None
    for line in MODEL_FGA.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("type "):
            current = stripped.split()[1]
        elif stripped.startswith("define ") and current is not None:
            relation, _, body = stripped.removeprefix("define ").partition(":")
            if "from child" in body:
                widened.add((current, relation.strip()))
    return frozenset(widened)


WIDENED = _child_widened_relations()
WIDENED_RELATIONS = frozenset(relation for _type, relation in WIDENED)


def _router_prefixes(tree: ast.Module) -> dict[str, str]:
    """`router variable -> URL prefix`, so a decorator's path can be resolved to the real route path."""
    prefixes = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if _callee(node.value) != "APIRouter":
            continue
        prefix = _const_str(_kwarg(node.value, "prefix")) or ""
        for target in node.targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix
    return prefixes


def _router_dependency_names(tree: ast.Module) -> dict[str, list[str]]:
    """`router variable -> the names of its router-level `Depends(...)` gates`."""
    deps: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if _callee(node.value) != "APIRouter":
            continue
        listed = _kwarg(node.value, "dependencies")
        names = []
        if isinstance(listed, ast.List):
            for element in listed.elts:
                if isinstance(element, ast.Call) and _callee(element) == "Depends" and element.args:
                    names.append(ast.unparse(element.args[0]).rsplit(".", 1)[-1])
        for target in node.targets:
            if isinstance(target, ast.Name):
                deps[target.id] = names
    return deps


def _route_decorator(fn: Function) -> tuple[str, str] | None:
    """`(router variable, path)` for the HTTP decorator on this function, if it carries one."""
    for decorator in fn.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        if decorator.func.attr not in HTTP_METHODS or not isinstance(decorator.func.value, ast.Name):
            continue
        path = _const_str(decorator.args[0]) if decorator.args else ""
        return decorator.func.value.id, path or ""
    return None


def _explicit_gates(fn: Function) -> list[tuple[str | None, str]]:
    """`(object type, relation)` for every in-body `require_*` gate whose relation is a literal.

    The object type is read off an f-string `obj=f"warehouse:{...}"`; `None` when the object is a
    variable or a settings attribute, which is answered by the relation-only fallback in `_clears`.
    """
    gates = []
    for call in _calls(fn):
        callee = _callee(call)
        if not callee.startswith("require_"):
            continue
        relation = _const_str(_kwarg(call, "relation"))
        if relation is None:
            continue  # an opaque gate — an ADDITIONAL restriction, so it neither clears nor condemns
        obj = _kwarg(call, "obj")
        object_type = None
        if isinstance(obj, ast.JoinedStr) and obj.values:
            head = _const_str(obj.values[0])
            if head and ":" in head:
                object_type = head.split(":", 1)[0]
        gates.append((object_type, relation))
    return gates


def _implicit_gate(path: str) -> tuple[str, str] | None:
    """The gate `catalog.api.fga_deps.authorize` applies to this route path, resolved by ITS OWN mapping.

    `_action_relation` is imported rather than reimplemented: a second copy of the op→relation policy is
    exactly the kind of sibling that drifts out of agreement with its twin.
    """
    match = ROUTE_WITH_ID.match(path)
    if match is None:
        return None
    resource = match["resource"]
    if resource not in _RESOURCES:
        return None
    fga_type = _FGA_TYPE[resource]
    return fga_type, _action_relation(fga_type, match["suffix"].rstrip("/"))


def _has_per_item_filter(fn: Function) -> bool:
    """The listing enumerates what the caller may see AND keeps only the items inside it.

    Both halves are required: `fga.list_objects` on its own is the access explorer's ANSWER, not a
    filter, so the filtering comprehension is what distinguishes "asked FGA" from "applied FGA".
    """
    asked = any(_callee(call) == "list_objects" for call in _calls(fn))
    filtered = any(
        isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) and any(generator.ifs for generator in node.generators)
        for node in ast.walk(fn)
    )
    return asked and filtered


def _listing_routes() -> list[tuple[str, Function, str, list[tuple[str | None, str]]]]:
    """Every `list_*` route the endpoints package mounts, with its route path and its gates."""
    routes = []
    for path, tree in _modules(ENDPOINTS):
        prefixes = _router_prefixes(tree)
        dependencies = _router_dependency_names(tree)
        module_functions = {fn.name: fn for fn in _functions(tree)}
        for fn in _functions(tree):
            if not fn.name.startswith("list_"):
                continue
            decorated = _route_decorator(fn)
            if decorated is None:
                continue
            router, route_path = decorated
            full = f"{prefixes.get(router, '')}{route_path}"
            gates = list(_explicit_gates(fn))
            # A router-level `Depends(gate)` is a gate every route under it clears, so its relation
            # counts as this route's — resolved by reading the dependency function in the same module.
            for dependency in dependencies.get(router, []):
                if dependency in module_functions:
                    gates.extend(_explicit_gates(module_functions[dependency]))
            implicit = _implicit_gate(full)
            if implicit is not None:
                gates.append(implicit)
            routes.append((_name(path, fn), fn, full, gates))
    return routes


def _clears(fn: Function, gates: list[tuple[str | None, str]]) -> bool:
    """A listing is sound when it filters per item, or when every gate it holds implies its contents."""
    if _has_per_item_filter(fn):
        return True
    if not gates:
        return False
    for object_type, relation in gates:
        widened = (object_type, relation) in WIDENED if object_type is not None else relation in WIDENED_RELATIONS
        if widened:
            return False
    return True


def test_a_listing_filters_per_item_or_rests_on_a_gate_that_implies_its_contents() -> None:
    """The sibling-name disclosure class: a listing opened by an upward-visibility relation must filter."""
    routes = _listing_routes()
    assert routes, "no list_* routes found — the endpoints walk is looking in the wrong place"
    assert WIDENED, f"no `from child` relations parsed out of {MODEL_FGA} — the model walk is broken"

    violations = []
    for name, fn, route_path, gates in routes:
        if name in LISTING_EXEMPT or _clears(fn, gates):
            continue
        described = ", ".join(f"{object_type or '?'}#{relation}" for object_type, relation in gates) or "no readable gate"
        violations.append(
            f"{name} (route {route_path}) applies no per-item authorization filter and is gated only by "
            f"[{described}] — a relation the model widens with `from child`, so it opens for anyone holding "
            f"`reader` on ONE item beneath it and answers with every sibling's name"
        )

    assert not violations, "listings that disclose siblings:\n  " + "\n  ".join(violations)


def test_the_listing_allowlist_carries_no_stale_or_unsound_exemption() -> None:
    """An exemption must name a live route, and must not be excusing a widened gate."""
    live = {name: (fn, gates) for name, fn, _path, gates in _listing_routes()}
    problems = []
    for name, reason in LISTING_EXEMPT.items():
        assert reason.strip(), f"{name} is exempt with no reason"
        entry = live.get(name)
        if entry is None:
            problems.append(f"{name} is exempt but is no longer a list_* route")
            continue
        _fn, gates = entry
        widened = [f"{t}#{r}" for t, r in gates if (t, r) in WIDENED or (t is None and r in WIDENED_RELATIONS)]
        if widened:
            problems.append(f"{name} is exempt, but its gate {widened} is child-widened — the route gate cannot stand in for a per-item filter")

    assert not problems, "stale or unsound listing exemptions:\n  " + "\n  ".join(problems)


# --------------------------------------------------------------------------- #
# (c) refusal parity — the question and the act must refuse the same things
# --------------------------------------------------------------------------- #


def _quality_doors() -> dict[str, frozenset[str]]:
    """Every function that runs `assert_quality`, mapped to the refusal helpers it calls.

    The population is "runs the publish gate", not the pair `gate`/`publish` by name: a third door that
    runs the same assertions inherits the same obligation the moment it is written.
    """
    doors = {}
    for path, tree in _modules(CATALOG_SRC):
        for fn in _functions(tree):
            callees = [_callee(call) for call in _calls(fn)]
            if "assert_quality" not in callees:
                continue
            doors[_name(path, fn)] = frozenset(callee for callee in callees if callee.startswith("refuse_"))
    return doors


def test_every_door_that_runs_the_quality_gate_raises_the_same_refusals() -> None:
    """The ask must agree with the act — a gate that under-reports approves what publish then refuses."""
    doors = _quality_doors()
    assert len(doors) >= 2, f"expected at least the question/act pair to run the quality gate, found {sorted(doors)}"

    union = frozenset().union(*doors.values())
    assert union, "no `refuse_*` helper is called by any quality-gate door — the refusal walk is broken"

    missing = {name: sorted(union - refusals) for name, refusals in doors.items() if refusals != union}
    assert not missing, "doors running the same quality gate that refuse different things:\n  " + "\n  ".join(
        f"{name} never calls {absent} — every other door running `assert_quality` does" for name, absent in missing.items()
    )


@pytest.mark.parametrize("root", [CATALOG_SRC, ENDPOINTS])
def test_the_walks_have_something_to_walk(root: pathlib.Path) -> None:
    """A gate whose population silently empties passes forever — pin that the tree is actually there."""
    assert root.is_dir(), f"{root} is missing — every gate in this file would pass vacuously"
    assert len(_modules(root)) > 5, f"{root} yielded too few modules to be the tree this gate is about"
