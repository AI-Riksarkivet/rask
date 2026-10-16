"""The chart WRITES the authorization model, so the store is not a third copy nobody checks.

[[LH-174]]. `model.fga` is the source of truth for every `can_*` the code derives, and nothing put it
in OpenFGA. Measured live 2026-09-18: the store's newest model defined 26/27/26 relations on
`warehouse`/`namespace`/`table` against 30/29/29 in the repo — so `warehouse#maintainer` existed in
the model the code reasons about and could not be written to the store at all.

THE FAILURE IS NOT SUBTLE AND IT BLOCKS UPGRADES. `rask-bootstrap-admin` is a Helm HOOK that grants
the maintenance service its rung, and it CrashLooped on
`Invalid tuple 'warehouse:lance_catalog#maintainer@user:service-maintenance'. Reason: relation
'warehouse#maintainer' not found` — so `make k3s-up` hung on it and the release wedged in
`pending-upgrade`, which the chart's own rules say refuses every later upgrade.

THREE COPIES, AND THE GATE ONLY EVER SAW TWO. `make fga-test` diffs `model.json` against `model.fga`.
The store is the third and was checked by nothing, which is exactly how it fell nine relations behind
without a single test going red.

NO FOURTH COPY. The model already ships INSIDE the service image — `service_kit/governed/auth/`
carries `model.fga`, `model.fga.yaml` and `model.json` in site-packages — so the hook reads it from
there. Rendering it into a ConfigMap would have created the fourth copy this row is about.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from chart_yaml import FAST_LOADER

from tests.unit.chart_render import PREREQ_ARGS, RAY_ARGS


REPO = Path(__file__).resolve().parents[2]


def _render(*extra: str) -> list[dict]:
    if not shutil.which("helm"):  # pragma: no cover - CI installs helm
        pytest.skip("helm not on PATH")
    cmd = ["helm", "template", "rask", str(REPO / "chart")]
    cmd += [*PREREQ_ARGS, *RAY_ARGS]
    cmd += ["--set-string", "auth.oidc.issuer=http://localhost:8080/dex"]
    cmd += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    cmd += ["--set", "image.localImages=true", *extra]
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(done.stderr)
    return [d for d in yaml.load_all(done.stdout, Loader=FAST_LOADER) if d]


def _jobs(docs: list[dict]) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Job"}


def _weight(job: dict) -> int:
    return int(job["metadata"]["annotations"]["helm.sh/hook-weight"])


@pytest.mark.parametrize("phase", ["install", "upgrade"])
def test_bootstrap_admin_runs_AFTER_the_jobs_it_waits_on(phase: str) -> None:
    """`bootstrap-admin` writes `warehouse:...#maintainer`, which the store accepts only once the model defining that
    relation is in it, and it waits for the catalog, which cannot start until `minio-scoped-users` has created its S3
    user. On an INSTALL the scoped-users Job is part of the manifest, never a hook: Helm runs post-install hooks only once
    every resource passes `--wait`, so a post-install scoped-users left the catalog unable to pass it and a fresh
    `helm install --wait` deadlocked ([[XC-152]]). On an UPGRADE it stays a hook weighted before bootstrap-admin, so a
    new identity's user exists before pods that use it roll ([[XC-133]], CI run 38217921913)."""
    jobs = _jobs(_render("--set", "fga.enabled=true", *(["--is-upgrade"] if phase == "upgrade" else [])))
    bootstrap = next((j for name, j in jobs.items() if "bootstrap-admin" in name), None)
    if bootstrap is None:
        pytest.skip("bootstrap-admin does not render here; the ordering it must beat is not present")
    [scoped] = [j for name, j in jobs.items() if "-minio-scoped-users" in name]
    hook = (scoped["metadata"].get("annotations") or {}).get("helm.sh/hook")
    if phase == "install":
        assert hook is None, f"on install the scoped-users Job is a {hook!r} hook; the catalog waits on it, so --wait deadlocks"
        return
    assert hook is not None and "pre-upgrade" in hook, hook
    before = {name: j for name, j in jobs.items() if ("model" in name and "openfga" in name) or "-minio-scoped-users" in name}
    late = {name: _weight(j) for name, j in before.items() if _weight(j) >= _weight(bootstrap)}
    assert not late, f"these run at or after bootstrap-admin (weight {_weight(bootstrap)}), which waits on them: {late}"
