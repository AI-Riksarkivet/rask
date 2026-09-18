"""Compute and the stage runners address the SAME Ray cluster.

[[CP-041]]. Two independent values keys name the Ray plane and nothing cross-checks them:
`ray.dashboardUrl` feeds `RAY_DASHBOARD_URL` (configmap.yaml:106-109), which is what `compute` prunes
job history against, serves its jobs board from and proxies `/api/serve` to; `medallion.rayAddress`
feeds `MEDALLION_RAY_ADDRESS` (medallion.yaml), which decides where the cascade SUBMITS.

WHEN THEY DIVERGE NOTHING FAILS, which is the whole problem. Every job still succeeds on the cluster
it was submitted to; it is simply counted and reclaimed by nobody, because the only reclaimer is
sweeping a different head. Measured 2026-09-18: the cascade submitted to an operator-managed
`rask-ray-head-svc` while compute still held a hand-set `http://ray-lance-head:8265` — an explicit
`env` row that SHADOWS `envFrom`, which the chart never renders and which `helm upgrade` therefore
never removed. It was the only reason the split was not already visible.

A RENDER GATE IS THE DURABLE HALF. Removing that row by hand fixed the estate for as long as nobody
sets it again; this fixes the chart, which is the thing that gets re-rendered.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from chart_yaml import FAST_LOADER


REPO = Path(__file__).resolve().parents[2]


def _render(*extra: str) -> list[dict]:
    if not shutil.which("helm"):  # pragma: no cover - CI installs helm
        pytest.skip("helm not on PATH")
    cmd = ["helm", "template", "rask", str(REPO / "chart")]
    cmd += ["--set-string", "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum"]
    cmd += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    cmd += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    cmd += ["--set", "image.localImages=true", *extra]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.load_all(out, Loader=FAST_LOADER) if d]


def _submit_targets(docs: list[dict]) -> dict[str, str]:
    """Where each stage runner SUBMITS — the explicit env row the medallion template renders."""
    found = {}
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for row in container.get("env", []):
                if row["name"] == "MEDALLION_RAY_ADDRESS" and row.get("value"):
                    found[doc["metadata"]["name"]] = row["value"]
    return found


def _introspect_target(docs: list[dict]) -> str:
    """Where compute PRUNES and introspects — via the shared ConfigMap, never its own env row."""
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and "RAY_DASHBOARD_URL" in (doc.get("data") or {}):
            return doc["data"]["RAY_DASHBOARD_URL"]
    raise AssertionError("no RAY_DASHBOARD_URL in the config ConfigMap")


def test_the_walk_sees_both_sides() -> None:
    """Without this the comparison below could hold over an empty set."""
    docs = _render("--set", "ray.cluster.enabled=true", "--set-string", "ray.dashboardUrl=")
    assert _submit_targets(docs), "no deployment renders MEDALLION_RAY_ADDRESS — this gate compares nothing"
    assert _introspect_target(docs), "no RAY_DASHBOARD_URL rendered"


def test_submission_and_introspection_name_ONE_cluster() -> None:
    docs = _render("--set", "ray.cluster.enabled=true", "--set-string", "ray.dashboardUrl=")
    submit = _submit_targets(docs)
    introspect = _introspect_target(docs)

    wrong = {name: target for name, target in submit.items() if target != introspect}
    assert not wrong, (
        f"these submit to a Ray the reclaimer does not sweep — their jobs accumulate counted by nobody. compute/prune reads {introspect!r}; {wrong}"
    )


def test_compute_takes_the_address_from_the_ConfigMap_and_not_its_own_env_row() -> None:
    """An explicit `env` row SHADOWS `envFrom`, so one rendered here would let compute drift from the
    ConfigMap silently — which is exactly the shape the hand-set value had."""
    for doc in _render("--set", "ray.cluster.enabled=true", "--set-string", "ray.dashboardUrl="):
        if doc.get("kind") != "Deployment" or not doc["metadata"]["name"].endswith("-compute"):
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            shadowing = [row["name"] for row in container.get("env", []) if row["name"] == "RAY_DASHBOARD_URL"]
            assert not shadowing, "compute renders its own RAY_DASHBOARD_URL, which shadows the ConfigMap the rest of the estate shares"
