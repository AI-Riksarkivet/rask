"""The annotator's auth env rides `auth.enabled` — the task plane must not be an open side-gate.

The projects endpoints gate can_claim/can_review/can_publish server-side, but the checker is
DELIBERATELY permissive when FGA is unconfigured (dev parity). So an auth-enabled estate whose
media template skipped the env would ship real doors on the catalog and none here — exactly the
gap the first FGA drive found. Render-pinned so the block cannot silently fall out of media.yaml.
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


def _render(**sets: str) -> list[dict]:
    if not shutil.which("helm"):  # pragma: no cover - CI installs helm
        pytest.skip("helm not on PATH")
    cmd = ["helm", "template", "rask", str(REPO / "chart")]
    # Since auth defaults ON (2026-08-06) every render needs identity values; the chart refuses OIDC
    # without a session secret ON PURPOSE, and that refusal has its own test in test_invariants.py.
    cmd += [*ESO_ARGS, *RAY_ARGS]
    cmd += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    cmd += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    # The chart REQUIRES an image registry unless the images are side-loaded into the node
    # (`rask.image` in _helpers.tpl): a bare `<component>:<tag>` is `docker.io/library/...`
    # and ImagePullBackOffs on any real cluster. These tests render the LOCAL shape, which is
    # the side-loaded one, so they opt in the same way `make k3s-up` does.
    cmd += ["--set", "image.localImages=true"]
    for key, value in sets.items():
        cmd += ["--set", f"{key.replace('__', '.')}={value}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.load_all(out, Loader=FAST_LOADER) if d]


def _annotator_env(docs: list[dict]) -> dict[str, str]:
    for doc in docs:
        name = doc.get("metadata", {}).get("name", "")
        # The SERVICE, not the zone image: `<release>-web-annotator` is the SvelteKit frontend.
        if doc.get("kind") != "Deployment" or not name.endswith("-annotator") or "-web-" in name:
            continue
        container = doc["spec"]["template"]["spec"]["containers"][0]
        return {e["name"]: e.get("value", "") for e in container.get("env", [])}
    raise AssertionError("no annotator Deployment rendered — is explorer.enabled on?")


def test_auth_enabled_wires_fga_and_oidc_onto_the_annotator() -> None:
    # allowHeadless: the render pin cares about the BACKEND env; the half-governed-deploy guard
    # (auth-consistency.yaml) otherwise rightly refuses auth.enabled without the UI's OIDC block.
    env = _annotator_env(_render(explorer__enabled="true", auth__enabled="true", auth__allowHeadless="true"))
    assert env.get("RASK_FGA_ENABLED") == "true"
    assert env.get("RASK_OIDC_ENABLED") == "true"
    assert "openfga" in env.get("RASK_FGA_API_URL", ""), env.get("RASK_FGA_API_URL")
    assert env.get("RASK_OIDC_AUDIENCE"), "the audience must come from dex.clientId"
