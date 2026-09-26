"""No catalog API module may raise the FLEET's exception taxonomy — the class gate for catalog-api-01.

The defect happened twice before this gate existed: `stores.py` imported `service_kit.exceptions`
in the original audit, and `members.py` repeated it months later (`RV-03`) — each time the module's
errors rendered as 4-key `about:blank#` problem bodies instead of the spec's six-key
`https://lance.org/problems/` envelope, because `register_handlers`'s DomainError handler caught
them before the lance_namespace problem handler could. Two occurrences is a class.

WHY THE SPLIT EXISTS AT ALL, so nobody "fixes" it by merging the taxonomies: the fleet's
`service_kit.exceptions` deliberately emits plain RFC 9457 with no Lance numeric `code` — gateway,
compute and notifications have no Lance contract to cite. The catalog DOES: its clients dispatch on
the spec's 24 codes, so every raise inside `catalog/api/` must come from `lance_namespace`, and the
translation to problem+json belongs to `install_problem_handlers`. The one legitimate import from
the fleet module is `register_handlers` itself (main.py keeps it installed as a net for shared
library code that raises DomainError — e.g. `UserStateConflict` escaping a future call site).

AST, not grep: an alias (`from service_kit import exceptions as exc`) or a multi-name import would
slip a substring check; the parse names the module and the imported names exactly.

AND RESOLVED, not only named. A fleet class also arrives by other routes — a module that re-exports
one, a subclass (`ProviderUnavailableError`, which the auth door catches), an attribute of an imported
package — so every imported name is resolved to the object it binds. A `DomainError` subclass may then
appear in `catalog/api` only as an `except` target, and that clause may not re-raise what it caught: bare,
by its name, or through a call on it. A caught exception stored and raised after the clause is past what
an AST walk can follow.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
from collections.abc import Iterator

from service_kit.exceptions import DomainError


API_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog" / "api"

#: The only names the catalog's API plane may take from the fleet taxonomy.
_ALLOWED_FROM_FLEET = {"register_handlers"}


def _fleet_imports(tree: ast.Module, path: pathlib.Path) -> list[str]:
    offences: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "service_kit.exceptions":
            bad = [alias.name for alias in node.names if alias.name not in _ALLOWED_FROM_FLEET]
            if bad:
                offences.append(f"{path.name}:{node.lineno} imports {bad} from service_kit.exceptions")
        if isinstance(node, ast.ImportFrom) and node.module == "service_kit":
            for alias in node.names:
                if alias.name == "exceptions":
                    offences.append(f"{path.name}:{node.lineno} imports the module from service_kit as {alias.asname or alias.name}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("service_kit.exceptions"):
                    offences.append(f"{path.name}:{node.lineno} imports the module wholesale as {alias.asname or alias.name}")
    return offences


def _bindings(tree: ast.Module) -> dict[str, object]:
    """Every name an absolute import in `tree` binds, resolved to the object it binds."""
    bound: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b` binds `a`; `import a.b as c` binds the submodule itself.
                module = importlib.import_module(alias.name)
                if alias.asname:
                    bound[alias.asname] = module
                else:
                    top = alias.name.partition(".")[0]
                    bound[top] = importlib.import_module(top)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            source = importlib.import_module(node.module)
            for alias in node.names:
                if alias.name != "*":
                    value = getattr(source, alias.name, None)
                    bound[alias.asname or alias.name] = value if value is not None else importlib.import_module(f"{node.module}.{alias.name}")
    return bound


def _unresolvable_imports(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """A relative or star import binds names the resolution above cannot see, so the gate refuses it."""
    return [
        f"{path.name}:{node.lineno} is a relative or star import the gate cannot resolve"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.level > 0 or any(alias.name == "*" for alias in node.names))
    ]


def _resolve(node: ast.expr, bound: dict[str, object]) -> object | None:
    if isinstance(node, ast.Name):
        return bound.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, bound)
        return None if base is None else getattr(base, node.attr, None)
    return None


def _is_fleet_class(value: object) -> bool:
    return isinstance(value, type) and issubclass(value, DomainError)


def _fleet_class_uses(tree: ast.Module, bound: dict[str, object], path: pathlib.Path) -> list[str]:
    """A fleet class named anywhere but an `except` target: raised, built, or passed on to be raised."""
    caught = {id(node) for handler in ast.walk(tree) if isinstance(handler, ast.ExceptHandler) and handler.type is not None for node in ast.walk(handler.type)}
    # Only the outermost node of a dotted name is resolved; `a` and `a.b` are parts of `a.b.C`.
    inner = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    return [
        f"{path.name}:{node.lineno} uses {ast.unparse(node)}, a fleet DomainError, outside an except clause"
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
        and isinstance(node.ctx, ast.Load)
        and id(node) not in caught | inner
        and _is_fleet_class(_resolve(node, bound))
    ]


def _raises_in(node: ast.AST) -> Iterator[ast.Raise]:
    """The `raise` statements in `node` that re-raise what the enclosing handler caught when bare.

    Not one in a nested function or class, which runs elsewhere, nor in a nested `except`, whose bare raise
    re-raises that clause's own exception.
    """
    if isinstance(node, ast.Raise):
        yield node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
        return
    for field, value in ast.iter_fields(node):
        if field == "handlers":
            continue
        for child in value if isinstance(value, list) else [value]:
            if isinstance(child, ast.AST):
                yield from _raises_in(child)


def _reraises(raised: ast.Raise, caught_as: str | None) -> bool:
    """Bare, by the clause's name, or through a call on that name: `exc.with_traceback(tb)` returns `exc`."""
    exc = raised.exc
    if exc is None:
        return True
    if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Attribute):
        exc = exc.func.value
    return isinstance(exc, ast.Name) and exc.id == caught_as


def _fleet_reraises(tree: ast.Module, bound: dict[str, object], path: pathlib.Path) -> list[str]:
    """The exception an `except <fleet class>` clause caught, raised again inside that clause."""
    offences: list[str] = []
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or handler.type is None:
            continue
        targets = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        if not any(_is_fleet_class(_resolve(target, bound)) for target in targets):
            continue
        for statement in handler.body:
            for raised in _raises_in(statement):
                if _reraises(raised, handler.name):
                    offences.append(f"{path.name}:{raised.lineno} re-raises the fleet exception its except clause caught")
    return offences


def _fleet_taxonomy_offences(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text())
    bound = _bindings(tree)
    offences = _fleet_imports(tree, path) + _unresolvable_imports(tree, path)
    offences += _fleet_class_uses(tree, bound, path)
    offences += _fleet_reraises(tree, bound, path)
    return offences


def test_the_gate_sees_the_api_plane_at_all() -> None:
    modules = list(API_ROOT.rglob("*.py"))
    assert len(modules) > 20, f"only {len(modules)} modules under catalog/api — the walk is not seeing the plane"


def test_no_api_module_raises_the_fleet_taxonomy() -> None:
    offences = [o for path in sorted(API_ROOT.rglob("*.py")) for o in _fleet_taxonomy_offences(path)]
    assert not offences, (
        "these catalog API modules import or raise the FLEET exception taxonomy — their errors render as "
        "4-key about:blank# bodies with no spec `code`, invisible to every generated Lance client. "
        "Raise `lance_namespace` errors and let install_problem_handlers translate:\n  " + "\n  ".join(offences)
    )


_CAUGHT_FLEET = "from service_kit.governed.oidc import ProviderUnavailableError\ntry:\n    pass\nexcept ProviderUnavailableError"

#: Sources the gate must flag, with their offence counts, and the uses the plane is allowed (0).
_SPELLINGS = {
    "from service_kit.exceptions import ServiceUnavailableError": 1,
    "from service_kit import exceptions as fleet": 1,
    "from service_kit import exceptions": 1,
    "import service_kit.exceptions as fleet": 1,
    "from service_kit.exceptions import register_handlers": 0,
    # A fleet class re-exported by another module, a subclass of one, and one reached by attribute.
    "from service_kit.governed.oidc import ServiceUnavailableError\nraise ServiceUnavailableError('x')": 1,
    "from service_kit.governed.oidc import ProviderUnavailableError\nraise ProviderUnavailableError('x')": 1,
    "from service_kit.governed.oidc import ProviderUnavailableError\nraise ProviderUnavailableError": 1,
    "from service_kit.governed.oidc import ProviderUnavailableError\nerror = ProviderUnavailableError('x')": 1,
    "import service_kit\nraise service_kit.exceptions.ServiceUnavailableError('x')": 1,
    # Names the resolution cannot see: each raise below is invisible to every other rule.
    "from .security import ProviderUnavailableError\nraise ProviderUnavailableError('x')": 1,
    "from service_kit.governed.oidc import *\nraise ProviderUnavailableError('x')": 1,
    # The caught fleet exception re-raised, bare or by name, renders the fleet body too.
    _CAUGHT_FLEET + ":\n    raise": 1,
    _CAUGHT_FLEET + " as exc:\n    raise exc": 1,
    _CAUGHT_FLEET + " as exc:\n    raise exc.with_traceback(None)": 1,
    _CAUGHT_FLEET + ":\n    try:\n        pass\n    finally:\n        raise": 1,
    # Catching one is allowed, and so is answering it with a Lance error; a nested handler re-raises its own.
    _CAUGHT_FLEET + ":\n    pass": 0,
    "from lance_namespace import ServiceUnavailableError\n" + _CAUGHT_FLEET + " as exc:\n    raise ServiceUnavailableError('x') from exc": 0,
    _CAUGHT_FLEET + ":\n    try:\n        pass\n    except ValueError:\n        raise": 0,
}


def test_the_gate_names_every_spelling_of_the_import(tmp_path: pathlib.Path) -> None:
    """The walk above is only as good as this: a spelling the parse does not match is a clean plane to the gate."""
    found = {}
    for source in _SPELLINGS:
        module = tmp_path / "module.py"
        module.write_text(source + "\n")
        found[source] = len(_fleet_taxonomy_offences(module))

    assert found == _SPELLINGS
