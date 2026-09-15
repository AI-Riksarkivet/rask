"""Goal condition 3, expressed in package metadata: the lakehouse may be DRIVEN BY a workflow engine.

[[LH-149]]. The import graph already said this — `medallion/workflow.py` is the only module-level
`import dapr.ext.workflow` in the service, every other site imports inside a function body, and
`services/medallion/tests/test_the_cascade_head_does_not_import_a_workflow_engine.py` proves it by
SUBPROCESS IMPORT rather than by reading source. The PACKAGE METADATA did not: `dapr-ext-workflow` sat
in medallion's unconditional `dependencies`, so `uv sync --package medallion` installed the engine
whether or not a deployment ran a workflow, and nothing could tell a driven-by from a depends-on.

MEASURED 2026-09-15 with the real resolver, because a `pyproject.toml` edit that the lockfile does not
honour would look identical: `uv export --frozen --no-dev --package medallion` names the engine **0**
times, and the same command with `--extra workflow` names it **2**.

THE TWO HALVES DRIFT APART SILENTLY AND THAT IS WHY BOTH ARE PINNED HERE. Making the dependency
optional without teaching the image to ask for it produces a build that succeeds and a service that
dies at import: `medallion/workflow.py` is the adapter and imports the engine at module scope, so the
producer and both stage runners would fail to start while the image looked perfectly assembled.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_MEDALLION = _ROOT / "services" / "medallion" / "pyproject.toml"
_IMAGE = _ROOT / ".docker" / "rest-catalog.dockerfile"
_ENGINE = "dapr-ext-workflow"


def _manifest() -> dict[str, object]:
    with _MEDALLION.open("rb") as handle:
        return tomllib.load(handle)


def test_the_engine_is_not_an_unconditional_dependency_of_the_cascade_head() -> None:
    """THE CONDITION ITSELF: a lakehouse service must not require a workflow engine to install."""
    project = _manifest()["project"]
    assert isinstance(project, dict)
    required = [dep for dep in project.get("dependencies", []) if _ENGINE in str(dep)]

    assert required == [], f"medallion requires a workflow engine to install, so the lakehouse DEPENDS ON one: {required}"


def test_the_engine_is_reachable_as_an_extra() -> None:
    """Optional must not mean absent — the adapter still needs a supported way to get it.

    Deleting the dependency outright would satisfy the test above and break every deployment that runs
    the cascade, which is the opposite of what condition 3 asks for.
    """
    project = _manifest()["project"]
    assert isinstance(project, dict)
    extras = project.get("optional-dependencies", {})
    assert isinstance(extras, dict)

    assert "workflow" in extras, f"no `workflow` extra declares the engine; medallion's adapter has no supported install path: {sorted(extras)}"
    assert any(_ENGINE in str(dep) for dep in extras["workflow"]), f"the `workflow` extra does not name the engine: {extras['workflow']}"


def test_the_image_that_runs_the_cascade_ASKS_for_the_extra() -> None:
    """The half that a metadata-only change breaks, and it breaks it invisibly.

    `.docker/rest-catalog.dockerfile` syncs per-package, which is exactly the resolution the extra
    gates. Both of its sync steps must pass `--extra workflow` — the first builds the dependency layer
    and the second installs the members, and a flag on only one of them produces a venv whose contents
    depend on layer-cache state rather than on the manifest.
    """
    # THE COMMANDS, joined across their line continuations — not the file text. Two earlier drafts of
    # this assertion counted substrings and were both wrong on a CORRECT file: once because a comment
    # says "uv sync" while describing the step, and once because the prose above the step names the
    # flag it is explaining. A gate that fails on a correct file gets edited until it passes, which is
    # how it stops guarding anything.
    commands = []
    current = ""
    for raw in _IMAGE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if current or "uv sync" in line:
            current = f"{current} {line.rstrip(chr(92)).strip()}".strip()
            if not line.endswith(chr(92)):
                commands.append(current)
                current = ""

    syncs = [c for c in commands if "uv sync" in c]
    assert len(syncs) == 2, f"the image's sync steps changed shape; re-check that each still asks for the extra: {syncs}"

    missing = [c for c in syncs if "--extra workflow" not in c]
    assert missing == [], (
        "a sync in the cascade image does not ask for the `workflow` extra — `medallion/workflow.py` "
        "imports the engine at module scope, so the producer and both stage runners die at import while "
        f"the image looks perfectly built: {missing}"
    )


def test_the_fleet_services_keep_their_own_engine_dependency() -> None:
    """Scope, pinned so a later tidy-up does not generalise this to services condition 3 never named.

    `ingest` and `flows` are FLEET services, not lakehouse ones. They each declare the engine for their
    own workflow, and the goal statement — "the lakehouse is four services: catalog, lineage, medallion,
    maintenance" — does not speak about them. Moving their pins too would be a change nobody asked for,
    and it would also silently remove the engine from the dev environment that medallion's own suite
    borrows today.
    """
    for service in ("ingest", "flows"):
        with (_ROOT / "services" / service / "pyproject.toml").open("rb") as handle:
            deps = tomllib.load(handle)["project"]["dependencies"]
        assert any(_ENGINE in str(dep) for dep in deps), f"{service} stopped declaring its own workflow engine — that is a separate decision from [[LH-149]]"


#: The four services the goal statement names as the lakehouse. Ray and the workflow engine are things
#: it may be DRIVEN BY; neither may be something it needs in order to install.
_LAKEHOUSE = ("catalog", "lineage", "medallion", "maintenance")


def _unconditional_dependencies(service: str) -> list[str]:
    with (_ROOT / "services" / service / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return [str(dep) for dep in project.get("dependencies", [])]


def test_no_lakehouse_service_requires_RAY_to_install() -> None:
    """The other half of condition 3, which reads "a workflow engine OR RAY" and was never pinned.

    MEASURED 2026-09-15 in the running `rask-medallion-producer`: none of the four declares ray or
    ray-kit unconditionally, and `import ray` fails in the image outright — the Ray lane reaches the
    cluster over the Jobs REST API rather than by importing the client. So this gate starts GREEN and
    its whole job is to stay that way: the coupling it refuses is one `uv add ray` away, and the
    failure would be invisible because everything would still work.
    """
    coupled = {
        service: [dep for dep in _unconditional_dependencies(service) if dep.split(">")[0].split("[")[0].split("=")[0].strip().lower() in {"ray", "ray-kit"}]
        for service in _LAKEHOUSE
    }
    offenders = {service: deps for service, deps in coupled.items() if deps}

    assert offenders == {}, f"a lakehouse service requires Ray to install, so the lakehouse DEPENDS ON it: {offenders}"


def test_no_lakehouse_service_imports_ray_at_MODULE_scope() -> None:
    """The metadata half is not enough on its own — an import can arrive without a manifest edit.

    A transitive install (service-kit extras, a sibling package) can put `ray` on the path, and then a
    module-level `import ray` couples the lakehouse to it while every pyproject still looks clean. The
    import graph is where the previous half of this row actually lived, so it is checked here too.
    """
    offenders: list[str] = []
    for service in _LAKEHOUSE:
        for path in (_ROOT / "services" / service / "src").rglob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.startswith(("import ray", "from ray")):
                    offenders.append(f"{path.relative_to(_ROOT)}:{number}: {line.strip()}")

    assert offenders == [], f"a lakehouse module imports Ray at module scope — it must be driven BY Ray, not depend on it: {offenders}"
