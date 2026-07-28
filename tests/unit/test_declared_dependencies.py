"""Every first-party import must be a DECLARED dependency of the package that makes it.

This gate exists because the defect class bit twice in one day, silently, in the same service:

- `annotator` imported `dapr.ext.fastapi` — its actor host — without declaring `dapr-ext-fastapi`
- `annotator` imported `lineage_kit.schemas` — its publish provenance — without declaring `lineage-kit`

Both worked perfectly in every test run and every local dev session, because a dev venv is built with
`uv sync --all-packages`: every workspace member is installed, so any member can import any other.
The images are not. `.docker/<name>.dockerfile` runs `uv sync --frozen --package <name>`, which
installs that member's DECLARED closure and nothing else — so the first of these would have shipped
an annotator with no actor routes, and the second an annotator that raises ImportError on publish.

Neither failure is visible to `make test`, `make lint`, `make typecheck` or CI. The only thing that
would have caught them is this: read what each package imports, read what it declares, compare.

Scope note: this checks FIRST-PARTY (workspace) imports only. Third-party undeclared imports are the
same bug, but the mapping from import name to distribution name is not mechanical (`dapr.ext.fastapi`
comes from `dapr-ext-fastapi`, `yaml` from `PyYAML`), and a guess-based map would produce false
failures — which is how a gate stops being trusted. `dapr-ext-fastapi` is therefore pinned by name in
its own case below rather than derived.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]

#: Workspace members, by the module name they are imported as. `packages/service-kit` ships
#: `service_kit`, so the mapping is dash→underscore rather than identity.
_MEMBER_DIRS = sorted([*(ROOT / "packages").glob("*/pyproject.toml"), *(ROOT / "services").glob("*/pyproject.toml")])


def _dist_name(pyproject: Path) -> str:
    return str(tomllib.loads(pyproject.read_text())["project"]["name"])


def _members() -> dict[str, str]:
    """module name -> distribution name, for every workspace member."""
    return {_dist_name(p).replace("-", "_"): _dist_name(p) for p in _MEMBER_DIRS}


def _declared(pyproject: Path) -> set[str]:
    """Distribution names this package declares, normalised. Extras are stripped —
    `service-kit[lancekit]` declares `service-kit`."""
    data = tomllib.loads(pyproject.read_text())
    out: set[str] = set()
    for raw in data.get("project", {}).get("dependencies", []):
        name = str(raw).split("[")[0].split(">")[0].split("<")[0].split("=")[0].split(";")[0].strip()
        if name:
            out.add(name.replace("_", "-").lower())
    return out


def _imported_modules(src: Path) -> set[str]:
    """Top-level module names imported anywhere under `src`, from the AST rather than a regex —
    a regex matches the word inside a docstring or a comment, which is how a gate cries wolf."""
    found: set[str] = set()
    for py in src.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:  # pragma: no cover - a syntax error is another gate's job to report
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def _closure(dist: str, declared_by: dict[str, set[str]], members_by_dist: dict[str, str]) -> set[str]:
    """Everything `dist` can legally import: its declared members, and theirs, transitively.

    Transitive on purpose — `annotator` declaring `service-kit` may legally import whatever
    `service-kit` itself declares, which is exactly how a real `uv sync --package` resolution behaves.
    """
    seen: set[str] = set()
    stack = [dist]
    while stack:
        current = stack.pop()
        for dep in declared_by.get(current, set()):
            if dep in members_by_dist and dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


_MEMBERS = _members()
_MEMBERS_BY_DIST = {v: v for v in _MEMBERS.values()}
_DECLARED_BY = {_dist_name(p): _declared(p) for p in _MEMBER_DIRS}


@pytest.mark.parametrize("pyproject", _MEMBER_DIRS, ids=lambda p: _dist_name(p))
def test_every_first_party_import_is_declared(pyproject: Path) -> None:
    """The `uv sync --frozen --package <name>` contract, checked statically.

    A failure here means the image for this package would raise ImportError at runtime while every
    other gate stayed green.
    """
    dist = _dist_name(pyproject)
    src = pyproject.parent / "src"
    if not src.is_dir():
        pytest.skip(f"{dist} has no src/ layout")

    allowed = _closure(dist, _DECLARED_BY, _MEMBERS_BY_DIST)
    own_module = dist.replace("-", "_")

    missing = {_MEMBERS[module] for module in _imported_modules(src) if module in _MEMBERS and module != own_module and _MEMBERS[module] not in allowed}

    assert not missing, (
        f"{dist} imports {sorted(missing)} but does not declare them (directly or transitively). "
        f"`uv sync --frozen --package {dist}` — what .docker builds — would not install them, so the "
        f"image raises ImportError while the dev venv (`--all-packages`) hides it."
    )


def test_the_annotator_declares_its_actor_host() -> None:
    """Pinned by name because the import name (`dapr.ext.fastapi`) does not mechanically yield the
    distribution name (`dapr-ext-fastapi`), so the general check above cannot derive it.

    `dapr.actor` ships inside `dapr`, which service-kit already pulls, so the actor CLASSES import
    fine without this — it is only the FastAPI integration that mounts `/dapr/*` and `/actors/*` that
    lives in the separate distribution. That is what made the omission invisible: the actors worked,
    and only the routes the sidecar calls back on were missing.
    """
    declared = _declared(ROOT / "services" / "annotator" / "pyproject.toml")
    assert "dapr-ext-fastapi" in declared, "the annotator hosts actors — without this its image mounts no /dapr/* routes"


def test_the_gate_can_actually_fail() -> None:
    """A gate nobody has seen fail is a gate nobody knows works. Feed the checker a package that
    imports a member it does not declare and prove it is rejected."""
    allowed = _closure("gateway", {"gateway": set()}, _MEMBERS_BY_DIST)
    assert "lineage-kit" not in allowed
    assert "lineage-kit" in _closure("x", {"x": {"lineage-kit"}}, _MEMBERS_BY_DIST)
