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

from tests.unit.chart_render import ESO_ARGS, RAY_ARGS


REPO = Path(__file__).resolve().parents[2]


def _render(*extra: str) -> list[dict]:
    if not shutil.which("helm"):  # pragma: no cover - CI installs helm
        pytest.skip("helm not on PATH")
    cmd = ["helm", "template", "rask", str(REPO / "chart")]
    cmd += [*ESO_ARGS, *RAY_ARGS]
    cmd += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
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


def test_it_runs_BEFORE_the_hook_that_writes_tuples() -> None:
    """Order is the whole point. `bootstrap-admin` writes `warehouse:...#maintainer`, which the store
    can only accept once the model defining that relation is in it — that ordering inversion is what
    CrashLooped and wedged the release."""
    jobs = _jobs(_render("--set", "fga.enabled=true"))
    writer = next(j for name, j in jobs.items() if "model" in name and "openfga" in name)
    bootstrap = next((j for name, j in jobs.items() if "bootstrap-admin" in name), None)

    if bootstrap is None:
        pytest.skip("bootstrap-admin does not render here; the ordering it must beat is not present")
    assert _weight(writer) < _weight(bootstrap), (
        f"the model write (weight {_weight(writer)}) runs at or after the tuple write "
        f"(weight {_weight(bootstrap)}), so the tuples land against a model that cannot express them"
    )
