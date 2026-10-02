"""A stage runner's IDENTITY must not be gated on an authorization toggle.

`MEDALLION_FGA_SERVICE_IDENTITY` is the author `sub` of every run a stage runner emits, and lineage
authorizes that author. Measured on the live estate 2026-09-08, rendered only with FGA on it was absent
from all three running stage runners, so the value fell back to its code default and every run claimed a
subject that holds no tuple: lineage refused the emit while the job landed its rows and reported success.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from chart_yaml import FAST_LOADER


REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "chart"
STAGE_IDENTITIES = ("service-bronze-to-silver", "service-silver-to-gold", "service-media-to-silver")


def _render(*extra: str) -> str:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    argv = [
        helm,
        "template",
        "rask",
        str(CHART),
        "--set",
        "image.localImages=true",
        "--set-string",
        "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum",
        "--set-string",
        "frontend.oidc.publicIssuer=http://localhost:8080/dex",
        "--set-string",
        "frontend.oidc.publicOrigin=http://localhost:8080",
        *extra,
    ]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout


def _stage_runner_env(rendered: str) -> dict[str, dict[str, str]]:
    """`{stage runner name: {env name: value}}` for every rendered stage runner Deployment."""
    out: dict[str, dict[str, str]] = {}
    for doc in yaml.load_all(rendered, Loader=FAST_LOADER):
        if not doc or doc.get("kind") != "Deployment":
            continue
        name = doc["metadata"]["name"]
        if not any(s in name for s in ("bronze-to-silver", "silver-to-gold", "media-to-silver")):
            continue
        container = doc["spec"]["template"]["spec"]["containers"][0]
        out[name] = {e["name"]: e.get("value", "") for e in container.get("env", [])}
    return out


def test_a_stage_runner_names_itself_with_fga_off() -> None:
    """The identity is who the runner IS. FGA decides what it may DO."""
    runners = _stage_runner_env(_render("--set", "medallion.ray=true", "--set", "auth.fgaEnabled=false"))
    assert runners, "no stage runner Deployments rendered — this pin is asserting nothing"
    for name, env in runners.items():
        claimed = env.get("MEDALLION_FGA_SERVICE_IDENTITY")
        assert claimed in STAGE_IDENTITIES, (
            f"{name} renders no identity with FGA off (got {claimed!r}) — its runs claim a code default as "
            "their author, which holds no tuple, and lineage refuses the emit while the job exits SUCCEEDED"
        )
