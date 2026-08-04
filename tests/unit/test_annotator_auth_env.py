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
    # The chart REQUIRES an image registry unless the images are side-loaded into the node
    # (`rask.image` in _helpers.tpl): a bare `<component>:<tag>` is `docker.io/library/...`
    # and ImagePullBackOffs on any real cluster. These tests render the LOCAL shape, which is
    # the side-loaded one, so they opt in the same way `make k3s-up` and the Tiltfile do.
    cmd += ["--set", "image.localImages=true"]
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
    raise AssertionError("no annotator Deployment rendered — is explorer.enabled on?")


def test_auth_enabled_wires_fga_and_oidc_onto_the_annotator() -> None:
    # allowHeadless: the render pin cares about the BACKEND env; the half-governed-deploy guard
    # (auth-consistency.yaml) otherwise rightly refuses auth.enabled without the UI's OIDC block.
    env = _annotator_env(_render(explorer__enabled="true", auth__enabled="true", auth__allowHeadless="true"))
    assert env.get("LANCE_FGA_ENABLED") == "true"
    assert env.get("LANCE_OIDC_ENABLED") == "true"
    assert "openfga" in env.get("LANCE_FGA_API_URL", ""), env.get("LANCE_FGA_API_URL")
    assert env.get("LANCE_OIDC_AUDIENCE"), "the audience must come from dex.clientId"


def test_auth_off_leaves_the_annotator_permissive_dev_parity() -> None:
    env = _annotator_env(_render(explorer__enabled="true"))
    assert "LANCE_FGA_ENABLED" not in env
    assert "LANCE_OIDC_ENABLED" not in env


def test_the_publish_identity_coordinates_ride_env_but_the_password_never_does() -> None:
    """The saga mints a fresh token per publish with the dex service account. Coordinates are env;
    the PASSWORD is seeded into OpenBao and fetched via the Dapr secret store, fail-closed — a
    rendered password in any env block would break the estate's secrets rule."""
    docs = _render(explorer__enabled="true", auth__enabled="true", auth__allowHeadless="true")
    env = _annotator_env(docs)
    assert env["MEDIA_PUBLISH_TOKEN_URL"].endswith("/dex/token")
    assert env["MEDIA_PUBLISH_CLIENT_ID"] == "lance-catalog"
    assert env["MEDIA_PUBLISH_USERNAME"] == "publisher@rask.internal"
    assert "MEDIA_PUBLISH_PASSWORD" not in env
    assert not any("publisher-oidc-password" in str(v) for v in env.values()), "the secret leaked into env"
    # …and the OTHER half of the contract: dex knows the account, OpenBao's seed carries the secret.
    flat = str(docs)
    assert "publisher@rask.internal" in flat
    assert "publisher-oidc-password=" in flat
