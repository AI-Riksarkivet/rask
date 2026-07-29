"""`dev.reload` must reach EVERY Python fleet service — and leak into production manifests nowhere.

This bug class has bitten FOUR times, each time silently, each time in a template that simply never
called the shared helper:

1. `dev.reload` was wired into `fleet.yaml` alone — excluding catalog and lineage, the very services
   Tilt's live_update syncs.
2. Fixed in `services.yaml`; `media.yaml` was missed. It inlined the module and flags into a single
   `command:` list with no `args:` split, so `lance.devReloadArgs` was never invoked. viewer, search
   and annotator therefore got files synced into the pod that uvicorn never re-read.
3. `controlplane.yaml` had the command/args split already and still never called the helper — found
   by the first run of THIS test, not by anyone noticing.
4. …which is the number this test exists to stop growing.

Every check is derived from the render, never from a list of service names: a new service is covered
the day it lands rather than the day someone remembers to add it here.

The failure mode is what makes it worth a gate: nothing errors. The chart renders, the pods start,
Tilt reports a successful sync, and the code in the container is stale. "It looked like it worked"
is the entire history of this repo's Tilt setup.

The production half is equally load-bearing. `--reload` in a production manifest means uvicorn
watches directories and restarts workers in a cluster nobody is editing, and `dev.reload` also
relaxes `readOnlyRootFilesystem` — so a leak is a security regression, not just noise.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "chart"


def _render(**sets: str) -> list[dict]:
    if not shutil.which("helm"):  # pragma: no cover - CI installs helm
        pytest.skip("helm not on PATH")
    cmd = ["helm", "template", "rask", str(CHART)]
    for k, v in sets.items():
        cmd += ["--set", f"{k.replace('__', '.')}={v}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout  # noqa: S603
    return [d for d in yaml.safe_load_all(out) if d]


def _uvicorn_containers(docs: list[dict]) -> list[tuple[str, dict]]:
    """(deployment, container) for every container whose entrypoint is uvicorn."""
    found = []
    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        for c in d["spec"]["template"]["spec"].get("containers", []):
            cmd = " ".join(str(x) for x in (c.get("command") or []))
            if "uvicorn" in cmd:
                found.append((d["metadata"]["name"], c))
    return found


def test_dev_reload_reaches_every_uvicorn_service() -> None:
    """Not a list of names — every uvicorn container the chart renders, whatever it is called."""
    docs = _render(media__enabled="true", frontend__enabled="true", dev__reload="true")
    containers = _uvicorn_containers(docs)
    assert containers, "no uvicorn containers rendered — the check would pass vacuously"

    missing = [name for name, c in containers if "--reload" not in (c.get("args") or [])]

    assert not missing, (
        f"dev.reload=true but these services get no --reload: {sorted(missing)}. "
        "Tilt will sync source into their pods and uvicorn will never re-read it — hot reload that "
        "looks configured and silently does nothing. The usual cause is a template inlining the "
        "module and flags into one `command:` list instead of splitting command/args and calling "
        "`lance.devReloadArgs`."
    )


def test_each_service_watches_its_own_package() -> None:
    """A reload-dir that does not exist in the image CRASHLOOPS the service — uvicorn refuses to
    start on a missing `--reload-dir` rather than skipping it. So each service must watch its own
    package plus the shared kits, never another service's."""
    docs = _render(media__enabled="true", dev__reload="true")
    for name, c in _uvicorn_containers(docs):
        args = c.get("args") or []
        if "--reload" not in args:
            continue  # its absence is the sibling test's job to report, with the right message
        dirs = [a.split("/")[-1] for a in args if "--reload-dir" in a]
        assert dirs, f"{name} has --reload but watches no directory — uvicorn would watch the CWD"
        assert "service_kit" in dirs, f"{name} does not watch service_kit — every image carries it"
        module = next((a.split(".main")[0] for a in (c.get("args") or []) if ".main:app" in a), None)
        if module:
            assert module in dirs, f"{name} runs {module}.main:app but does not watch the {module} package"


def test_production_manifests_carry_no_reload() -> None:
    """The default render is what ships. A `--reload` here means uvicorn watches directories and
    restarts workers in a cluster nobody is editing."""
    docs = _render(media__enabled="true", frontend__enabled="true")
    leaked = [name for name, c in _uvicorn_containers(docs) if any("--reload" in str(a) for a in (c.get("args") or []))]
    assert not leaked, f"production manifests leak --reload: {sorted(leaked)}"


def test_production_keeps_a_read_only_root_filesystem() -> None:
    """`dev.reload` relaxes `readOnlyRootFilesystem` so live_update can write into the container.
    Leaking THAT is a security regression, which is why it is asserted separately from the flag."""
    docs = _render(media__enabled="true", frontend__enabled="true")
    writable = [f"{name}/{c['name']}" for name, c in _uvicorn_containers(docs) if (c.get("securityContext") or {}).get("readOnlyRootFilesystem") is False]
    assert not writable, f"production containers have a writable root filesystem: {sorted(writable)}"


def test_dev_reload_relaxes_the_root_filesystem_where_it_must() -> None:
    """The other direction: live_update cannot write into a read-only container, so `dev.reload`
    must relax it — otherwise the sync fails and, again, nothing says so."""
    docs = _render(media__enabled="true", dev__reload="true")
    still_ro = [name for name, c in _uvicorn_containers(docs) if (c.get("securityContext") or {}).get("readOnlyRootFilesystem") is True]
    assert not still_ro, f"dev.reload=true but these keep a read-only rootfs, so live_update cannot write: {sorted(still_ro)}"
