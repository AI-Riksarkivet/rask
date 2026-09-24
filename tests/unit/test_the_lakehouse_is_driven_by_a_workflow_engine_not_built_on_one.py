"""The lakehouse may be DRIVEN BY a workflow engine; it may not DEPEND ON one.

Owner's clause 3 (2026-09-10): "It is NOT coupled to a workflow engine or to Ray. Dapr Workflow and
Ray are things the lakehouse can be driven BY, never things it depends ON." The Ray half is held by
`test_no_service_depends_on_a_compute_engine`. This is the workflow half, and it was held by nothing.

THE DISTINCTION IS THE IMPORT'S SCOPE, which is what makes it testable. A module-scope
`import dapr.ext.workflow` is a hard dependency: the service cannot start without the engine
installed, and the failure is an ImportError at boot that names a package rather than a capability. A
nested one is a capability the service reaches for when configured to — `stage_runner.py` imports it
only `if settings.ray_enabled`, inside a `try`, leaving `app.state.workflow_runtime = None`
otherwise, so a medallion with no engine runs and simply schedules nothing.

Measured 2026-09-24 across catalog, lineage, medallion and maintenance: seven engine imports, six
nested, one at module scope — `medallion/workflow.py`, which is the ADAPTER, the module whose whole
job is to define the workflows. That one is legitimate precisely because every import OF it is
itself nested (five call sites, all inside functions), so the engine never enters a service's import
graph unless something asks for it at runtime.

NO ADAPTER LIST, because a list of exemptions is where couplings hide. A module that imports the
engine at module scope IS an adapter by that fact, and the gate then demands the thing that makes it
safe: nothing may import an adapter at module scope either. Add a second adapter and it is held to
the same rule without editing this file.
"""

from __future__ import annotations

import ast
import pathlib
import tomllib
from pathlib import Path

import pytest


REPO = pathlib.Path(__file__).resolve().parents[2]

#: The owner's four. Deliberately NOT the sibling gate's list, which names `notifications` (phase 3)
#: and omits `medallion` — so medallion, the one lakehouse service that touches a workflow engine at
#: all, was checked by nothing.
LAKEHOUSE = ("catalog", "lineage", "medallion", "maintenance")

#: Import prefixes that ARE a workflow engine. `durabletask` is the runtime Dapr Workflow sits on, so
#: reaching it directly couples just as hard.
ENGINES = ("dapr.ext.workflow", "durabletask")

#: The modules allowed to hold the engine at module scope, named rather than inferred.
#:
#: A SELF-DERIVING RULE HAS NO TEETH HERE, which is measured rather than argued: deriving "an adapter
#: is any module with a module-scope engine import" means the edit this gate exists to catch — adding
#: that import to `stage_runner.py` — simply makes stage_runner an adapter and the gate stays green.
#: An explicit set fails in BOTH directions: a new module-scope import is an unlisted adapter, and a
#: retired one is a stale entry. Adding a name here is a deliberate, reviewable act.
ADAPTERS = frozenset({"medallion.workflow"})

#: Distribution names that BIND a workflow engine into a service's resolution, lock and image.
ENGINE_DISTS = frozenset({"dapr-ext-workflow", "durabletask", "temporalio", "prefect", "apache-airflow"})


def _modules() -> list[tuple[str, pathlib.Path, str]]:
    """`(service, path, importable dotted name)` for every non-test module in the four services."""
    out = []
    for service in LAKEHOUSE:
        src = REPO / "services" / service / "src"
        if not src.exists():
            continue
        for path in sorted(src.rglob("*.py")):
            dotted = ".".join(path.relative_to(src).with_suffix("").parts).removesuffix(".__init__")
            out.append((service, path, dotted))
    return out


def _imports(path: pathlib.Path) -> list[tuple[str, int, int]]:
    """`(imported module, line, column)` for every import in the file. Column 0 means module scope."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        found.extend((name, node.lineno, node.col_offset) for name in names if name)
    return found


def _holders() -> dict[str, list[str]]:
    """Dotted names that import an engine AT MODULE SCOPE, whatever they are called."""
    holders: dict[str, list[str]] = {}
    for service, path, dotted in _modules():
        for name, line, col in _imports(path):
            if col == 0 and name.startswith(ENGINES):
                holders.setdefault(dotted, []).append(f"{service}:{path.name}:{line} {name}")
    return holders


def test_exactly_the_named_adapters_hold_the_engine() -> None:
    """The set that may import an engine at module scope is fixed, and checked in both directions."""
    holders = _holders()
    unlisted = sorted(set(holders) - ADAPTERS)
    assert not unlisted, (
        f"these modules import a workflow engine at module scope but are not named adapters: "
        f"{ {name: holders[name] for name in unlisted} }. A module-scope import makes the engine a "
        f"start-up requirement of the service that imports it."
    )
    stale = sorted(ADAPTERS - set(holders))
    assert not stale, f"these are named as engine adapters but hold no engine any more — drop them: {stale}"


def test_the_gate_can_see_engine_imports() -> None:
    """A control: with no engine referenced anywhere, every assertion below passes by vacuum."""
    referenced = [f"{service}:{path.name}:{line}" for service, path, _ in _modules() for name, line, _col in _imports(path) if name.startswith(ENGINES)]
    assert referenced, f"no lakehouse service references {ENGINES} — the gate lost its subject"


def _declared(pyproject: Path) -> tuple[set[str], dict[str, set[str]]]:
    """`(required, {extra: names})` — kept APART, because the distinction is the whole property."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project") or {}

    def names(specs: list[str]) -> set[str]:
        out = set()
        for spec in specs:
            name = spec.split(";")[0].strip()
            for stop in ("[", ">", "<", "=", "!", "~", " "):
                name = name.split(stop)[0]
            if name:
                out.add(name.strip().lower())
        return out

    return names(list(project.get("dependencies") or [])), {extra: names(list(specs)) for extra, specs in (project.get("optional-dependencies") or {}).items()}


@pytest.mark.parametrize("service", LAKEHOUSE)
def test_no_lakehouse_service_REQUIRES_a_workflow_engine(service: str) -> None:
    """REQUIRED only. An OPTIONAL extra is how "can be driven by" is expressed in packaging.

    A required dependency forces the engine into the lock and every image built from it, so the
    service cannot be installed without it. An extra leaves that to whoever assembles the deployable:
    `uv export --package medallion` names the engine 0 times, and with `--extra workflow` 2 times, so
    the lakehouse's resolution stays free of it and only the image that runs the cascade takes it on.
    A first cut of this gate merged the two and called medallion coupled — it is not; the extra is
    the mechanism, not a loophole.
    """
    pyproject = REPO / "services" / service / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip(f"services/{service} has no pyproject")
    required, _extras = _declared(pyproject)
    bound = required & ENGINE_DISTS
    assert not bound, (
        f"services/{service} REQUIRES a workflow engine {sorted(bound)}. Move it to an optional extra: "
        f"a required dependency is depending on an engine, not being driven by one."
    )


@pytest.mark.parametrize("service", LAKEHOUSE)
def test_an_engine_extra_is_installed_by_the_image_that_runs_it(service: str) -> None:
    """The other hop, and the one that fails SILENTLY.

    Every engine import in these services is nested inside a `try` that logs and continues — that is
    correct, since a service with no engine must still run. But it means an image that resolves the
    extra away does not crash: it comes up healthy with `workflow_runtime = None` and simply never
    executes a stage, and the only evidence is one log line at boot. So an extra that carries the
    engine is not optional for the image that ships the cascade, and this pins that pairing.
    """
    pyproject = REPO / "services" / service / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip(f"services/{service} has no pyproject")
    _required, extras = _declared(pyproject)
    engine_extras = sorted(name for name, dists in extras.items() if dists & ENGINE_DISTS)
    if not engine_extras:
        pytest.skip(f"services/{service} declares no workflow-engine extra")

    dockerfiles = [path for path in sorted((REPO / ".docker").glob("*.dockerfile")) if f"--package {service}" in path.read_text(encoding="utf-8")]
    assert dockerfiles, f"services/{service} is built by no dockerfile — the pairing cannot be checked"
    for dockerfile in dockerfiles:
        # COMMENTS STRIPPED FIRST. `.docker/rest-catalog.dockerfile` explains the flag in prose two
        # lines above using it, so a raw substring search is satisfied by the explanation alone —
        # measured while mutation-checking this gate, deleting the real flag left it green.
        body = "\n".join(line.split("#", 1)[0] for line in dockerfile.read_text(encoding="utf-8").splitlines())
        missing = [extra for extra in engine_extras if f"--extra {extra}" not in body]
        assert not missing, (
            f"{dockerfile.name} installs services/{service} without {missing}, which carries the workflow "
            f"engine. The lazy import then fails inside a `try`, the pod comes up healthy, and the "
            f"cascade silently never runs."
        )


def test_an_engine_import_outside_an_adapter_is_lazy() -> None:
    """Every engine import sits inside a function, except in the adapter that exists to hold it."""
    offenders = []
    for _service, path, dotted in _modules():
        if dotted in ADAPTERS:
            continue
        offenders += [
            f"{path.relative_to(REPO)}:{line} imports {name} at module scope" for name, line, col in _imports(path) if col == 0 and name.startswith(ENGINES)
        ]
    assert not offenders, (
        "a module-scope engine import makes the engine a START-UP requirement of a lakehouse service, "
        "so the service cannot run without it and the failure names a package rather than a "
        "capability:\n  " + "\n  ".join(offenders)
    )


def test_nothing_imports_an_adapter_at_module_scope() -> None:
    """What makes the adapter's own module-scope import safe — and the half a list of exemptions loses.

    An adapter may hold the engine because nothing pulls it in unless a caller asks at runtime. One
    module-scope `from medallion.workflow import ...` anywhere and the engine is back in the service's
    import graph, with the adapter exemption still looking satisfied.
    """
    assert _holders(), "no module imports a workflow engine at module scope — the adapter check lost its subject"
    offenders = []
    for _service, path, dotted in _modules():
        if dotted in ADAPTERS:
            continue
        offenders += [
            f"{path.relative_to(REPO)}:{line} imports the adapter {name} at module scope" for name, line, col in _imports(path) if col == 0 and name in ADAPTERS
        ]
    assert not offenders, (
        f"these pull a workflow-engine adapter ({sorted(ADAPTERS)}) into the import graph at module "
        f"scope, which re-couples the service the adapter exists to keep free:\n  " + "\n  ".join(offenders)
    )
