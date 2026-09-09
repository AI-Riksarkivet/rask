"""A stage runner's IDENTITY must not be gated on an authorization toggle.

`MedallionSettings.catalog_service_identity` already records this lesson, in its own docstring:
"Deliberately NOT `fga_service_identity`, which carries the same value but is rendered only when FGA
is on. Authentication and authorization are different questions, and coupling them means a governed
estate running `auth.enabled: true` with FGA off cannot authenticate at all."

`ray_submit` then reaches for `fga_service_identity` anyway, to fill the stage job's
`LINEAGE_SERVICE_ID` — an AUTHENTICATION claim taken from an AUTHORIZATION-gated value. Measured on
the live estate 2026-09-08: `MEDALLION_FGA_SERVICE_IDENTITY` is absent from all three running stage
runners, so the value falls back to its code default and every stage job would claim a subject the
door has never heard of.

The failure is silent by construction. `emit()` returns early on an unset LINEAGE_URL, so the estate
that has not wired `stage_lineage_url` sees nothing at all; the estate that wires it sees its jobs
land their rows and lose their provenance, which is the 2026-07-13 trainer incident's exact shape.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


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
    for doc in yaml.load_all(rendered, Loader=yaml.CSafeLoader):
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
            f"{name} renders no identity with FGA off (got {claimed!r}) — its stage jobs claim a code "
            "default at lineage's door, which the allowlist has never heard of, and the emit is refused "
            "while the job exits SUCCEEDED"
        )


def test_a_stage_lane_is_told_where_to_post_its_provenance() -> None:
    """A stage job that cannot reach lineage emits nothing, and reports that as success.

    `stage_lineage_url` is empty in code so an unwired estate posts to no guessed address. The chart
    is not a guess: it knows its own lineage Service, exactly as it already does for the train lane's
    `MEDALLION_TRAIN_LINEAGE_URL`.
    """
    runners = _stage_runner_env(_render("--set", "medallion.ray=true"))
    for name, env in runners.items():
        url = env.get("MEDALLION_STAGE_LINEAGE_URL", "")
        assert url.startswith("http"), f"{name} submits Ray stage jobs with nowhere to post provenance (got {url!r}) — the whole lane is unlineaged, silently"
