"""A scoped storage identity the chart PROVISIONS must be the one its service actually presents.

`rustfs-scoped-users.yaml` creates a least-privilege RustFS user and policy for the maintenance,
medallion and lineage planes on every install and upgrade. All three are then OFF by default —
`rustfs.<plane>AccessKey: ""` falls back to the tenant ROOT credential — so the chart builds the
identity, provisions it, and then hands the service `rustfsadmin` anyway.

MEASURED ON THE RUNNING ESTATE 2026-09-08, reading each pod's real environment rather than the
templates: catalog, lineage, ingest and viewer all present `rustfsadmin`. The operator had armed
maintenance and medallion by hand in a values file; lineage — whose policy is the TIGHTEST of the
three, needing no PutObject at all — was still root because nobody knew to name it.

This is the shape `rask.isRealDeployment` was written to end, in its own words: "BOTH SIGNALS ARE
FACTS THE DEPLOY ALREADY DEPENDS ON, never a flag someone must remember to arm. A guard reachable
only by remembering an opt-in protects the installs that did not need protecting." A provisioned
identity nobody selected is that guard.

The ordering is the reason it shipped off, and it was a real one: the provisioning hook ran
`post-upgrade`, so a chart that defaulted the key ON would roll the pods onto a credential that did
not exist yet. That is fixed by the hook running before the roll, not by leaving every install on the
root key.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "chart"

#: `{env var naming the identity: the workload that presents it}` — one row per plane the chart
#: provisions a scoped RustFS user for in `rustfs-scoped-users.yaml`.
PROVISIONED_PLANES = {
    "MAINTENANCE_S3_ACCESS_KEY_ID": "maintenance",
    "MEDALLION_S3_ACCESS_KEY_ID": "the medallion plane",
    "LINEAGE_S3_ACCESS_KEY_ID": "lineage",
}


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


def _root_key() -> str:
    return str(yaml.safe_load((CHART / "values.yaml").read_text())["rustfs"]["accessKey"])


def _rendered_identities(rendered: str) -> dict[str, set[str]]:
    """`{env var: every value any workload renders for it}`."""
    found: dict[str, set[str]] = {k: set() for k in PROVISIONED_PLANES}
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") not in {"Deployment", "StatefulSet"}:
            continue
        for container in doc["spec"]["template"]["spec"].get("containers", []):
            for env in container.get("env", []):
                if env["name"] in found and "value" in env:
                    found[env["name"]].add(env["value"])
    return found


def test_no_provisioned_plane_falls_back_to_the_storage_root() -> None:
    """The default install must not hand a service the tenant root key it has an identity for."""
    root = _root_key()
    identities = _rendered_identities(_render())
    for var, plane in PROVISIONED_PLANES.items():
        rendered = identities[var]
        assert rendered, f"{var} renders nowhere — this pin is asserting nothing about {plane}"
        assert root not in rendered, (
            f"{plane} presents the tenant ROOT credential {root!r} on a default install, while "
            f"`rustfs-scoped-users.yaml` provisions a least-privilege user for it in the same release. "
            "Root by default is not a deployment choice anyone made."
        )


def test_the_identity_exists_before_the_pods_that_present_it_roll() -> None:
    """Arming the identity is only safe if provisioning precedes the roll.

    A `post-upgrade` hook creates the RustFS user AFTER the Deployments are updated, so pods restart
    holding a credential the object store has never heard of and 403 until the hook catches up. That
    window is why the keys shipped empty; closing it is what makes the default above safe.
    """
    for doc in yaml.safe_load_all(_render()):
        if not doc or doc.get("kind") != "Job":
            continue
        if "scoped-users" not in doc["metadata"]["name"]:
            continue
        phases = doc["metadata"]["annotations"]["helm.sh/hook"].split(",")
        assert "pre-upgrade" in phases, (
            f"{doc['metadata']['name']} provisions the scoped users only in {phases} — an upgrade rolls "
            "the pods onto a credential that does not exist yet, which is exactly why every plane "
            "shipped defaulted to root"
        )
        return
    pytest.fail("no scoped-users provisioning Job rendered — the identities are not provisioned at all")
