"""The chart is consumed by a GitOps reconciler, and these are the two properties that requires.

Neither was true before 2026-08-04, and both failed the same way: silently at render time, loudly
much later in a cluster nobody was watching.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from chart_yaml import FAST_LOADER

from tests.unit.chart_render import ESO_ARGS, RAY_ARGS


REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "chart"


def _helm() -> str:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    return helm


def _render(*sets: str) -> subprocess.CompletedProcess[str]:
    argv = [_helm(), "template", "rask", str(CHART)]
    # Since auth defaults ON (2026-08-06) every render needs identity values; the chart refuses OIDC
    # without a session secret ON PURPOSE, and that refusal has its own test in test_invariants.py.
    argv += [*ESO_ARGS, *RAY_ARGS]
    argv += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    argv += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    for value in sets:
        argv += ["--set", value]
    return subprocess.run(argv, capture_output=True, text=True, timeout=300)


def test_the_object_store_does_not_inherit_the_generic_APP_memory_ceiling() -> None:
    """RustFS sits under the whole lakehouse and named no `resources` tier, so it inherited
    `default` — 512Mi, sized for a stateless FastAPI shell. Measured 2026-08-05 on the dev estate:
    370Mi at IDLE and **33 OOMKills**, each one taking every Lance read and write in the cluster
    down with it. The failure is invisible in review (the values file simply lacks a key) and reads
    in the cluster as "the lakehouse is flaky".
    """
    rendered = _render("image.localImages=true", "minio.enabled=true")
    assert rendered.returncode == 0, rendered.stderr

    stores = [
        doc
        for doc in yaml.load_all(rendered.stdout, Loader=FAST_LOADER)
        if doc
        and doc.get("kind") == "StatefulSet"
        and doc["metadata"]["labels"].get("app.kubernetes.io/component") != "age"
        and any(c["name"] == "minio" for c in doc["spec"]["template"]["spec"]["containers"])
    ]
    assert stores, "the object store did not render — this test would pass vacuously"

    def _mib(quantity: str) -> int:
        text = str(quantity)
        return int(text.removesuffix("Gi")) * 1024 if text.endswith("Gi") else int(text.removesuffix("Mi"))

    for store in stores:
        for container in store["spec"]["template"]["spec"]["containers"]:
            limit = container["resources"]["limits"]["memory"]
            assert _mib(limit) >= 2048, f"the object store is capped at {limit} — it OOMKills under real load"


def test_the_recoverable_drop_plane_is_actually_WIRED_into_the_catalog() -> None:
    """#122. The trash/undrop/purge plane — ~500 lines, 42 tests, an undrop UI — was inert in every
    chart deployment: `LANCE_TRASH_GRACE_DAYS` was rendered NOWHERE, while two chart comments
    discussed it as an operator knob. Every drop destroyed bytes immediately, `undrop` had nothing to
    restore, and `maintenance.trashPurge` shipped as a toggle whose input could never exist.
    """
    rendered = _render("image.localImages=true")
    assert rendered.returncode == 0, rendered.stderr

    for doc in yaml.load_all(rendered.stdout, Loader=FAST_LOADER):
        if not doc or doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            if container["name"] != "catalog":
                continue
            env = {e["name"]: e.get("value") for e in container.get("env", [])}
            assert env.get("LANCE_TRASH_GRACE_DAYS") == "7", "the grace period never reaches the catalog"
            return
    raise AssertionError("no catalog container rendered — this test would pass vacuously")
