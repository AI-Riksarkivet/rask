"""The ESO manifests name an API version the operator still serves.

MEASURED, not guessed. `external-secrets` 0.20.x serves `external-secrets.io/v1` and marks `v1beta1`
`served: false`, so the chart's manifests were REJECTED by the cluster outright:

    no matches for kind "ExternalSecret" in version "external-secrets.io/v1beta1"

That is the estate's SANCTIONED secret-distribution path — the one thing that gets a credential to a
pod with no Dapr sidecar (the Ray head/workers, the web zones, every runner). It is also a path
nothing exercised: `externalSecrets.enabled` defaults false, so every render and every test skipped
these documents, and the break was invisible until an operator turned it on in production — the worst
possible moment to discover that secrets have no transport.

The gate is a STRING check on the rendered manifests rather than a live API probe, deliberately: it
has to fail in CI, where no cluster exists. `v1beta1` is asserted absent by name because that is the
specific version this estate shipped and the one a copy-paste from old docs reintroduces.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml


REPO = pathlib.Path(__file__).resolve().parents[2]

#: The version external-secrets serves today. `v1beta1` was removed from the served set in 0.17+.
SERVED = "external-secrets.io/v1"


def _rendered_eso() -> list[dict]:
    import shutil
    import subprocess

    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not pathlib.Path(helm).exists():
        pytest.skip("helm not available")
    out = subprocess.run(  # noqa: S603
        [
            helm,
            "template",
            str(REPO / "chart"),
            "--set",
            "image.localImages=true",
            "--set",
            "externalSecrets.enabled=true",
            "--set",
            "frontend.oidc.sessionSecret=probe-secret-that-is-long-enough-x",
            "--set-string",
            "frontend.oidc.publicIssuer=http://localhost:8080/dex",
            "--set-string",
            "frontend.oidc.publicOrigin=http://localhost:8080",
            "--set-string",
            "dex.issuer=http://localhost:8080/dex",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d and d.get("kind") in {"ExternalSecret", "SecretStore", "ClusterSecretStore"}]


def test_every_eso_document_names_the_served_api() -> None:
    docs = _rendered_eso()
    assert docs, "externalSecrets.enabled=true rendered no ESO documents — this gate sees nothing to check"
    wrong = sorted({f"{d['kind']}/{d['metadata']['name']}={d['apiVersion']}" for d in docs if d["apiVersion"] != SERVED})
    assert not wrong, f"these name an apiVersion the operator does not serve, so the cluster refuses them: {wrong}"


def test_the_ray_credential_is_among_what_eso_syncs() -> None:
    """The whole point of turning ESO on here: a pod with no sidecar still gets its secret."""
    keys: set[str] = set()
    for doc in _rendered_eso():
        if doc["kind"] == "ExternalSecret" and "infra-credentials" in doc["metadata"]["name"]:
            keys |= set(((doc["spec"].get("target") or {}).get("template") or {}).get("data") or {})
    assert {"ray-compute-access-key", "ray-compute-secret-key"} <= keys, f"ESO would not deliver the Ray plane's credential; it syncs: {sorted(keys)}"
