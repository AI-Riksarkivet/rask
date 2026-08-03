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


REPO = Path(__file__).resolve().parents[2]


def _render(**sets: str) -> list[dict]:
    if not shutil.which("helm"):  # pragma: no cover - CI installs helm
        pytest.skip("helm not on PATH")
    cmd = ["helm", "template", "rask", str(REPO / "chart")]
    for key, value in sets.items():
        cmd += ["--set", f"{key.replace('__', '.')}={value}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _annotator_env(docs: list[dict]) -> dict[str, str]:
    for doc in docs:
        name = doc.get("metadata", {}).get("name", "")
        # The SERVICE, not the zone image: `<release>-web-annotator` is the SvelteKit frontend.
        if doc.get("kind") != "Deployment" or not name.endswith("-annotator") or "-web-" in name:
            continue
        container = doc["spec"]["template"]["spec"]["containers"][0]
        return {e["name"]: e.get("value", "") for e in container.get("env", [])}
    raise AssertionError("no annotator Deployment rendered — is media.enabled on?")


def test_auth_enabled_wires_fga_and_oidc_onto_the_annotator() -> None:
    # allowHeadless: the render pin cares about the BACKEND env; the half-governed-deploy guard
    # (auth-consistency.yaml) otherwise rightly refuses auth.enabled without the UI's OIDC block.
    env = _annotator_env(_render(media__enabled="true", auth__enabled="true", auth__allowHeadless="true"))
    assert env.get("LANCE_FGA_ENABLED") == "true"
    assert env.get("LANCE_OIDC_ENABLED") == "true"
    assert "openfga" in env.get("LANCE_FGA_API_URL", ""), env.get("LANCE_FGA_API_URL")
    assert env.get("LANCE_OIDC_AUDIENCE"), "the audience must come from dex.clientId"


def test_auth_off_leaves_the_annotator_permissive_dev_parity() -> None:
    env = _annotator_env(_render(media__enabled="true"))
    assert "LANCE_FGA_ENABLED" not in env
    assert "LANCE_OIDC_ENABLED" not in env
