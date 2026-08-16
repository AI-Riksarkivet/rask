"""Gate 7 (R3) render proofs — Ray token auth static wiring in the chart.

The live tokenless-rejection proof runs at the cluster gates; these tests pin what
`helm template` can prove offline:

  * auth ON  -> the token Secret renders (key `auth_token`, the KubeRay convention),
    the RayService hands it to the kuberay >= 1.6.0 operator NATIVELY via
    rayClusterConfig.authOptions (mode token + secretName) — the operator injects the
    RAY_AUTH_MODE/RAY_AUTH_TOKEN pair into every Ray container and its RayService
    controller authenticates its own /api/serve reconcile calls from the same Secret —
    and the pair lands on EXACTLY the Ray-talking fleet services (compute, R22 — the
    orchestrator died at P7a; search-api/volumes-api/core-api died in the R6/R20 wave)
    — least privilege: gateway/controlplane never talk to Ray and never see it.
  * auth OFF (default) -> zero auth manifests/env anywhere (current behavior intact).
  * externalSecrets ON -> the ESO ExternalSecret owns the same-named Secret and the
    static one is skipped (no plaintext token in the chart).
  * prod signal (openbao.devMode=false) -> the render FAILS CLOSED when Ray deploys
    without auth, and on the REPLACE-ME token placeholder — same pattern as the
    dapr-app-token / infra-credentials guards.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "chart"

RAY_TALKERS = {"rask-compute"}
NEVER_RAY = {"rask-gateway", "rask-controlplane"}

# Values that satisfy the OTHER prod guards (infra-credentials, dapr-app-token) so the
# prod-signal tests isolate the ray-auth guard.
_PROD_BASE = (
    "singleTenant.enabled=true",
    "openbao.devMode=false",
    "age.password=x",
    "rustfs.secretKey=x",
    "dapr.appToken=x",
)


def _helm(*set_values: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", "rask", str(CHART)]
    # Since auth defaults ON (2026-08-06) every render needs identity values; the chart refuses OIDC
    # without a session secret ON PURPOSE, and that refusal has its own test in test_invariants.py.
    argv += ["--set-string", "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum"]
    argv += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    argv += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    # The chart REQUIRES an image registry unless the images are side-loaded into the node
    # (`rask.image` in _helpers.tpl): a bare `<component>:<tag>` is `docker.io/library/...`
    # and ImagePullBackOffs on any real cluster. These tests render the LOCAL shape, which is
    # the side-loaded one, so they opt in the same way `make k3s-up` does.
    argv += ["--set", "image.localImages=true"]
    for value in set_values:
        argv += ["--set", value]
    return subprocess.run(argv, capture_output=True, text=True, check=check)  # noqa: S603


def _docs(rendered: str) -> list[str]:
    return rendered.split("\n---")


def _name(doc: str) -> str:
    m = re.search(r"^\s*name:\s*(\S+)", doc, re.MULTILINE)
    return m.group(1) if m else "?"


def test_auth_on_renders_secret_head_env_and_exactly_the_ray_talking_fleet() -> None:
    rendered = _helm("singleTenant.enabled=true", "ray.auth.enabled=true").stdout
    docs = _docs(rendered)

    secrets = [d for d in docs if re.search(r"^kind: Secret$", d, re.MULTILINE) and "rask-ray-auth-token" in d]
    assert len(secrets) == 1, "exactly one ray-auth-token Secret must render"
    assert "auth_token:" in secrets[0], "Secret data key must be auth_token (the KubeRay operator convention)"

    rayservices = [d for d in docs if "kind: RayService" in d]
    assert len(rayservices) == 1
    head = rayservices[0]
    # The kuberay >= 1.6.0 NATIVE wiring: authOptions hands the operator our pre-existing
    # Secret (it skips generating its own when secretName is set, injects the env pair into
    # every Ray container, and the RayService controller auths its reconcile calls with it).
    assert re.search(r"authOptions:\s*\n\s*mode: token\s*\n\s*secretName: rask-ray-auth-token", head), (
        "RayService must carry rayClusterConfig.authOptions (mode token + secretName -> the chart's Secret)"
    )
    assert "RAY_AUTH_TOKEN" not in head, "no manual RAY_AUTH_* env on the Ray containers — the 1.6+ operator injects the pair from authOptions"
    assert not re.search(r"RAY_AUTH_TOKEN['\"]?\s*:\s*['\"]?\w", head), "token must never appear as a plaintext value (e.g. in serveConfigV2 runtime_env)"

    with_pair = {_name(d) for d in docs if "kind: Deployment" in d and "RAY_AUTH_MODE" in d}
    assert with_pair == RAY_TALKERS, f"the env pair must land on exactly {sorted(RAY_TALKERS)}, got {sorted(with_pair)}"
    for d in docs:
        if "kind: Deployment" in d and _name(d) in NEVER_RAY:
            assert "RAY_AUTH" not in d, f"{_name(d)} never talks to Ray and must not carry the token (least privilege)"


def test_auth_off_default_renders_no_auth_surface_at_all() -> None:
    rendered = _helm("singleTenant.enabled=true").stdout
    for raw in _docs(rendered):
        # ray-auth-token.yaml renders comment-only prose when auth is off (repo pattern);
        # the invariant is about MANIFEST content, so strip comment lines first.
        doc = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("#"))
        assert "RAY_AUTH_MODE" not in doc, f"auth env leaked into {_name(doc)} with ray.auth.enabled=false"
        assert not (re.search(r"^kind: (Secret|ExternalSecret)$", doc, re.MULTILINE) and "ray-auth-token" in doc), (
            "ray-auth-token Secret must not render with auth off"
        )


def test_external_secrets_owns_the_token_and_the_static_secret_is_skipped() -> None:
    rendered = _helm("singleTenant.enabled=true", "ray.auth.enabled=true", "externalSecrets.enabled=true").stdout
    docs = _docs(rendered)
    static = [d for d in docs if re.search(r"^kind: Secret$", d, re.MULTILINE) and "rask-ray-auth-token" in d]
    assert not static, "with ESO on, no plaintext token Secret may ship in the chart"
    es = [d for d in docs if "kind: ExternalSecret" in d and "rask-ray-auth-token" in d]
    assert len(es) == 1, "the ESO path must sync the same-named Secret from Vault"
    assert "property: ray-auth-token" in es[0], "token must come from the established secretPath (OpenBao KV property ray-auth-token)"
    assert "auth_token:" in es[0], "the synced Secret must keep the auth_token data key the consumers reference"


@pytest.mark.parametrize("eso", [False, True], ids=["static-secret", "external-secrets"])
def test_the_token_renders_for_an_EXTERNAL_ray_cluster(eso: bool) -> None:
    """singleTenant.enabled=false — the shape this estate calls NORMAL — must still get the credential.

    configmap.yaml:101 states it outright: "rask's Ray is managed outside this repo, so an external
    address is the NORMAL case and the in-cluster head is the opt-in". A fleet talking to a Ray it does
    not host still has to present a bearer token, so the credential's existence cannot depend on whether
    THIS chart also deploys the cluster.

    REGRESSION: both templates opened with `and .Values.ray.enabled .Values.singleTenant.enabled`, while
    the CONSUMING `rask.rayAuthEnv` fires on `ray.auth.enabled` alone. Every test above pins
    singleTenant.enabled=true, so the suite only ever rendered the opt-in shape and stayed green while
    the default one was broken. Switching auth on for real (2026-08-16) put rask-compute into
    CreateContainerConfigError against a Secret that never rendered, and hung the release in
    pending-upgrade behind `helm --wait`.
    """
    args = ["singleTenant.enabled=false", "ray.auth.enabled=true"]
    if eso:
        args.append("externalSecrets.enabled=true")
    docs = _docs(_helm(*args).stdout)

    kind = "ExternalSecret" if eso else "Secret"
    owning = [d for d in docs if re.search(rf"^kind: {kind}$", d, re.MULTILINE) and "rask-ray-auth-token" in d]
    assert len(owning) == 1, f"external-Ray render must still produce exactly one {kind} holding the token"
    assert "auth_token:" in owning[0], "the data key consumers reference via secretKeyRef must survive"


@pytest.mark.parametrize("single_tenant", [False, True], ids=["external-ray", "in-cluster-ray"])
@pytest.mark.parametrize("eso", [False, True], ids=["static-secret", "external-secrets"])
def test_every_secretKeyRef_to_the_token_has_something_that_creates_it(single_tenant: bool, eso: bool) -> None:
    """The load-bearing symmetry: nothing may REFERENCE the token Secret unless the render also CREATES it.

    This is the invariant the individual gate tests keep missing, because each one checks a single
    template under a single combination. A `secretKeyRef` to an absent Secret is not a render error —
    kubelet accepts the pod and then wedges it in CreateContainerConfigError, so it fails at deploy time
    on a cluster, which is the most expensive place to find it.
    """
    args = ["ray.auth.enabled=true", f"singleTenant.enabled={str(single_tenant).lower()}"]
    if eso:
        args.append("externalSecrets.enabled=true")
    docs = _docs(_helm(*args).stdout)

    referrers = {_name(d) for d in docs if "rask-ray-auth-token" in d and "secretKeyRef" in d}
    creators = [d for d in docs if re.search(r"^kind: (Secret|ExternalSecret)$", d, re.MULTILINE) and "rask-ray-auth-token" in d]
    assert referrers, "expected at least the rayClient fleet to reference the token in this shape"
    assert len(creators) == 1, f"{sorted(referrers)} reference rask-ray-auth-token but {len(creators)} manifests create it"


def test_prod_render_fails_closed_when_ray_deploys_without_auth() -> None:
    """openbao.devMode=false is the prod signal: an unauthenticated Ray dashboard is remote
    code execution (jobs/Serve REST take arbitrary entrypoints), so the render must abort —
    the same fail-closed pattern as dapr-app-token.yaml / infra-credentials.yaml."""
    proc = _helm(*_PROD_BASE, check=False)
    assert proc.returncode != 0, "prod render with an open Ray must fail"
    assert "ray.auth.enabled=false on a prod render" in proc.stderr


def test_prod_render_fails_on_the_replace_me_token_placeholder() -> None:
    proc = _helm(*_PROD_BASE, "ray.auth.enabled=true", "ray.auth.token=REPLACE-ME-with-a-real-secret", check=False)
    assert proc.returncode != 0
    assert "ray.auth.token is still the REPLACE-ME placeholder" in proc.stderr


def test_prod_render_with_auth_enabled_is_clean() -> None:
    proc = _helm(*_PROD_BASE, "ray.auth.enabled=true", check=False)
    assert proc.returncode == 0, proc.stderr
