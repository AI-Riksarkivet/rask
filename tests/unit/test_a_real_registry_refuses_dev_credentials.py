"""Shipping to a real registry on dev credentials fails the render.

Q17-13 / §F2-9, whose title is the finding: "refuse well-known defaults in the base chart, NOT ONLY on
devMode=false". Four of the five values it names already have a guard — `age.password`,
`dex.clientSecret` and `minio.secretKey` in `infra-credentials.yaml`, and `dapr.appToken` in
`dapr-app-token.yaml`, which even carries the two-case shape (placeholder always, dev default on the
prod signal). The fifth, `openbao.devMode`, is the SIGNAL rather than a credential.

WHAT THIS PINS IS THEREFORE NOT THE PRESENCE OF A GUARD BUT THE BREADTH OF ITS SIGNAL. A refusal
reachable only by remembering to flip `openbao.devMode` protects the installs that did not need
protecting: every value named here is published in `chart/values.yaml`, so an operator who ships
production without that flag ships credentials anyone holding the repository already has.

`image.localImages` IS A SIGNAL THE OPERATOR CANNOT FORGET, because the deployment already depends on
it. `make k3s-up` renders with `localImages=true` for side-loaded images; anything pulling from a real
registry leaves it false, and the chart already refuses a bare `<component>:<tag>` in that case since
that is docker.io. So "a real registry" is not a new flag to set — it is a fact about where the images
come from, and dev credentials there are indefensible whatever `devMode` says.

DELIBERATELY NOT `auth.enabled`: it defaults ON (2026-08-06, so a forgotten values file cannot install
an ungoverned estate), and `make k3s-up` runs governed with dev credentials on purpose. Keying there
would break the local loop, which is how a guard gets disabled rather than satisfied.
"""

from __future__ import annotations

import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


#: The values a fresh install carries, and what each authenticates.
WELL_KNOWN = {
    "minio.secretKey": "minioadmin",
    "age.password": "lance",
    "dapr.appToken": "lance-dev-dapr-app-token-override-me",
}
#: What every render needs before it can get as far as a credential guard.
IDENTITY = (
    "--set-string frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum",
    "--set-string frontend.oidc.publicIssuer=http://localhost:8080/dex",
    "--set-string frontend.oidc.publicOrigin=http://localhost:8080",
)


def _render(*set_values: str) -> tuple[bool, str]:
    """`(rendered_ok, message)` — a render that FAILS is the assertion here, not an error."""
    import subprocess

    helm = pathlib.Path(__file__).resolve().parents[2] / ".localbin/helm"
    exe = str(helm) if helm.exists() else "helm"
    chart = pathlib.Path(__file__).resolve().parents[2] / "chart"
    argv = [exe, "template", str(chart), *[a for v in IDENTITY for a in v.split(" ", 1)], *set_values]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    return proc.returncode == 0, proc.stderr


def test_the_local_loop_still_renders() -> None:
    """`make k3s-up` runs governed, on dev credentials, with side-loaded images — on purpose. A guard
    that breaks this is a guard someone deletes."""
    ok, err = _render("--set", "image.localImages=true")
    assert ok, f"the side-loaded local render broke, which is the one thing this guard must not do:\n{err[:600]}"


@pytest.mark.parametrize("value", sorted(WELL_KNOWN))
def test_a_real_registry_on_a_dev_credential_is_REFUSED(value: str) -> None:
    """The case a single opt-in signal cannot see: a real deployment whose `openbao.devMode` is
    untouched, shipping the estate's published secrets with nothing red."""
    ok, err = _render(
        "--set",
        "image.repository=ghcr.io/example/rask",
        "--set",
        "image.localImages=false",
        "--set",
        "openbao.devMode=true",
    )
    assert not ok, (
        f"the chart rendered a REAL-REGISTRY deployment while {value} is still {WELL_KNOWN[value]!r} — "
        "every credential guard keys on openbao.devMode, which this render leaves at its default"
    )
    assert value.split(".")[-1] in err or "dev credential" in err.lower(), (
        f"the render was refused but the message does not name {value}, so an operator cannot act on it:\n{err[:600]}"
    )


def test_overriding_the_credentials_lets_a_real_registry_render() -> None:
    """The refusal must be satisfiable by fixing what it names, not only by turning the render local.

    `openbao.devMode` moves with them: on a real registry a dev-mode OpenBao is itself refused — it is
    in-memory and auto-unsealed behind a fixed root token, so anything in the cluster reads every
    secret the estate holds, and overriding four values while leaving that open fixes nothing.
    """
    overrides: list[str] = []
    for key in WELL_KNOWN:
        overrides += ["--set-string", f"{key}=a-real-secret-value-32-chars-long"]
    ok, err = _render(
        "--set",
        "image.repository=ghcr.io/example/rask",
        "--set",
        "image.localImages=false",
        "--set",
        "openbao.devMode=false",
        *overrides,
    )
    assert ok, f"a real-registry render with every credential overridden was still refused:\n{err[:600]}"


def test_a_DEV_MODE_openbao_does_not_reach_a_real_registry() -> None:
    """The signal's own value. `openbao.devMode: true` is the default, and on a real deployment it is
    the widest of these: not one credential but the store that holds all of them, unsealed."""
    ok, err = _render(
        "--set",
        "image.repository=ghcr.io/example/rask",
        "--set",
        "image.localImages=false",
        "--set-string",
        "minio.secretKey=a-real-secret-value-32-chars-long",
        "--set-string",
        "age.password=a-real-secret-value-32-chars-long",
        "--set-string",
        "dapr.appToken=a-real-secret-value-32-chars-long",
    )
    assert not ok, "a real-registry render kept an in-memory auto-unsealed OpenBao on its fixed root token"
    assert "devMode" in err, f"the refusal does not name devMode:\n{err[:400]}"


def test_the_dev_REGISTRY_is_not_a_real_registry() -> None:
    """`make dev-registry` pulls via `localhost:5000` while Dagger pushes to `172.17.0.1:5000` — the
    in-cluster dev loop, on addresses unroutable from anywhere but the node. Refusing it would break a
    supported local path, and a guard that breaks the local loop is a guard someone deletes."""
    for registry in ("localhost:5000", "127.0.0.1:5000", "172.17.0.1:5000"):
        ok, err = _render("--set", f"image.repository={registry}", "--set", "image.localImages=false")
        assert ok, f"the dev registry {registry} was refused as a real deployment:\n{err[:400]}"
