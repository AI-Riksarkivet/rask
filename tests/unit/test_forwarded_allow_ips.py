"""The trusted-proxy CIDR must be settable, must reach every fleet pod, and must never be `*`.

open_fastapi-audit — "The gateway's `--forwarded-allow-ips=127.0.0.1` can never match the Ingress
controller that calls it, and the client-controlled X-Forwarded-For is forwarded to every backend
untouched".

`127.0.0.1` is the value a gateway would use if the proxy were a sidecar in its own pod. The proxy is
the Ingress controller, in another pod on another IP, so the match can never succeed and uvicorn
correctly ignores every forwarded header it is sent. The image says so itself
(`.docker/gateway.dockerfile`: "--forwarded-allow-ips MUST be the cluster edge's CIDR at deploy time
… never '*'"), and the chart gave a deployer no way to satisfy that obligation short of replacing the
whole `extraArgs` list.

TWO CORRECTIONS THE FINDING MAKES ABOUT ITSELF are honoured here. It is LATENT — nothing in the
Python estate reads `request.client.host`, `request.url.scheme` or any `x-forwarded-*`, so there is no
audit line or rate limit collapsing today. And "there is no override path" is wrong: `extraArgs` is
itself a values key. What was missing is a dedicated knob, which is what this gates.

THE CMD-vs-command QUESTION, decided (owner, 2026-08-27). Six dockerfiles end with a CMD carrying
`--proxy-headers --forwarded-allow-ips`, and the chart overrides `command`/`args` for every one of
them — so production ran none of it, while six comments read as deployment requirements. The chart now
renders the posture the images document, for every fleet service rather than the gateway alone: once
the gateway stamps the chain itself, a backend that trusts the gateway resolves the REAL client, so
the trap the finding describes is defused rather than left armed one hop further in.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest
import yaml


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _fleet_config, _rendered_docs  # noqa: E402, F401


REPO = pathlib.Path(__file__).resolve().parents[2]
CHART = REPO / "chart"


def _fleet_containers(docs: list[dict]) -> dict[str, dict]:
    """Containers the chart starts with `uvicorn`, keyed by component."""
    found = {}
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            if "uvicorn" in (container.get("command") or []):
                found[container["name"]] = container
    return found


def _images_that_document_the_posture() -> list[str]:
    """Dockerfile stems whose CMD carries `--proxy-headers`.

    DERIVED FROM THE IMAGES, which is the finding's own framing: "a deploy-time obligation the image
    states explicitly is unmet by the chart". So the expected set is not a list here — it is whatever
    the dockerfiles currently claim, and an image that starts or stops claiming it moves this gate with
    it. The lance plane (catalog, lineage, maintenance, the medallion apps) is absent because its
    images document no such posture; widening to it would be a different claim than this finding makes.
    """
    return sorted(p.stem for p in (REPO / ".docker").glob("*.dockerfile") if "--proxy-headers" in p.read_text())


_CONTAINERS = _fleet_containers(_rendered_docs())
_DOCUMENTED = [stem for stem in _images_that_document_the_posture() if stem in _CONTAINERS]

assert _CONTAINERS, "no uvicorn-started container rendered — this file would pass vacuously"
assert len(_DOCUMENTED) >= 5, f"only {_DOCUMENTED} images document --proxy-headers; the finding names six"


@pytest.mark.parametrize("component", _DOCUMENTED)
def test_every_fleet_pod_runs_the_posture_its_image_documents(component: str) -> None:
    """Six dockerfiles document `--proxy-headers`; the chart overrode the CMD and ran none of them."""
    argv = " ".join(_CONTAINERS[component].get("args") or [])
    assert "--proxy-headers" in argv, (
        f"{component} runs without --proxy-headers, so uvicorn ignores the forwarded chain entirely "
        "— while its dockerfile CMD documents the flag as a deployment requirement"
    )
    assert "--forwarded-allow-ips" in argv, f"{component} declares no trusted proxy, so it falls back to uvicorn's 127.0.0.1 default"


def test_the_trusted_proxy_cidr_is_a_values_key() -> None:
    """A deployer must be able to satisfy the dockerfile's obligation without replacing extraArgs."""
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert "forwardedAllowIps" in values, "there is no dedicated knob for the trusted-proxy CIDR"

    docs = _rendered_docs("forwardedAllowIps=10.42.0.0/16")
    argv = " ".join(_fleet_containers(docs)["gateway"].get("args") or [])
    assert "--forwarded-allow-ips=10.42.0.0/16" in argv, f"the key does not reach the pod: {argv}"


def test_the_chart_REFUSES_a_wildcard_trusted_proxy() -> None:
    """`*` trusts every peer, so any client's `x-forwarded-for` becomes the resolved client IP.

    Refused at RENDER time rather than documented: the dockerfiles already say "never '*'" in a
    comment, and the whole point of this finding is that a comment is not a mechanism.
    """
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not pathlib.Path(helm).exists():
        pytest.skip("helm not available")
    argv = [
        helm, "template", str(CHART),
        "--set", "image.localImages=true",
        "--set-string", "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum",
        "--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex",
        "--set-string", "frontend.oidc.publicOrigin=http://localhost:8080",
        "--set-string", "forwardedAllowIps=*",
    ]  # fmt: skip
    result = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode != 0, "the chart rendered with forwardedAllowIps='*' — every client can then forge its own source IP"
    assert "forwardedAllowIps" in result.stderr, f"the refusal does not name the offending key: {result.stderr[-300:]}"
