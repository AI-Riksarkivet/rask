"""The ESO manifests name an API version the operator still serves.

MEASURED, not guessed. `external-secrets` 0.20.x serves `external-secrets.io/v1` and marks `v1beta1`
`served: false`, so the chart's manifests were REJECTED by the cluster outright:

    no matches for kind "ExternalSecret" in version "external-secrets.io/v1beta1"

That is the estate's SANCTIONED secret-distribution path — the one thing that gets a credential to a
pod with no Dapr sidecar (the Ray head/workers, the web zones, every runner). While ESO was an
opt-in nothing exercised, the break stayed invisible until an operator turned it on in production — the
worst possible moment to discover that secrets have no transport. It is a prerequisite now ([[XC-004]]).

The gate is a STRING check on the rendered manifests rather than a live API probe, deliberately: it
has to fail in CI, where no cluster exists. `v1beta1` is asserted absent by name because that is the
specific version this estate shipped and the one a copy-paste from old docs reintroduces.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml
from chart_yaml import FAST_LOADER

from tests.unit.chart_render import ESO_ARGS, RAY_ARGS


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
            *ESO_ARGS,
            *RAY_ARGS,
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
    return [d for d in yaml.load_all(out, Loader=FAST_LOADER) if d and d.get("kind") in {"ExternalSecret", "SecretStore", "ClusterSecretStore"}]


def test_every_eso_document_names_the_served_api() -> None:
    docs = _rendered_eso()
    assert docs, "the render has no ESO documents — this gate sees nothing to check"
    wrong = sorted({f"{d['kind']}/{d['metadata']['name']}={d['apiVersion']}" for d in docs if d["apiVersion"] != SERVED})
    assert not wrong, f"these name an apiVersion the operator does not serve, so the cluster refuses them: {wrong}"
