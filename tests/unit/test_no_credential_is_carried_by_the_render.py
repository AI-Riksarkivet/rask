"""No credential is minted by, carried in, or accepted into the chart's render ([[XC-004]]).

A render is stored whole in the release (`helm get manifest`, `hooks`, `values`), every revision of it, so
a credential that passes through one is a secret at rest outside the store. Three ways one used to: a
template minting it (`randAlphaNum` for the Ray token), a value carrying it (`dapr.appToken`,
`frontend.oidc.sessionSecret`, `minio.secretKey`, ...), and a template rendering it into a Secret or a Job.
The credentials now live in the store and reach pods through ESO, a prerequisite the chart checks for.
The dev store's are generated in-cluster; `test_the_dev_openbao_is_seeded_by_its_own_pod.py` runs that half.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, OIDC_ARGS, REPO, render_chart_text, render_text


#: The two cluster-less renders an operator makes: the chart's dev defaults and the local estate's overlay,
#: each with every credential consumer on (the Ray token was the one the chart minted).
_RENDERS = {
    "dev": (*DEFAULT_ARGS, "--set", "ray.auth.enabled=true", "--set", "singleTenant.enabled=true"),
    "local": ("--set", "image.localImages=true", "-f", str(REPO / "chart/values-local.yaml"), "--set", "ray.auth.enabled=true"),
}


@pytest.mark.parametrize("overlay", sorted(_RENDERS))
def test_a_cluster_less_render_mints_no_credential(overlay: str) -> None:
    """Two renders of one overlay are byte-identical, so nothing in them was generated at render time."""
    first = render_chart_text(REPO / "chart", *_RENDERS[overlay])
    second = render_chart_text(REPO / "chart", *_RENDERS[overlay])

    assert "name: rask-ray-auth-token" in first, f"{overlay}: the Ray token's consumer is off, so this would pass vacuously"
    assert first == second, f"{overlay}: two renders differ, so the render minted something the release then stores"


def test_a_credential_passed_as_a_value_is_refused_naming_it() -> None:
    """A credential the chart once read from a value is refused, by name, on any render."""
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_text(*DEFAULT_ARGS, "--set-string", "dapr.appToken=a-token", "--set-string", "minio.secretKey=a-root-secret")

    assert "A CREDENTIAL IS NOT A CHART VALUE: dapr.appToken, minio.secretKey." in refused.value.stderr


def test_a_real_deployment_refuses_the_bundled_dev_idp() -> None:
    """The bundled Dex is one of the two exemptions (owner, 2026-10-06), fenced to dev renders: a real one refuses it."""
    real = ("--set", "image.repository=ghcr.io/example/rask", "--set", "openbao.devMode=false", "--set", "signing.provisioned=true")
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_text(*real, "--set", "nats.auth.provisioned=true")

    assert "dex.enabled=true — the bundled dev IdP" in refused.value.stderr


def test_a_render_for_a_cluster_without_the_external_secrets_operator_fails_naming_it() -> None:
    """ESO is a prerequisite rask never installs: a render for a cluster that does not serve it stops and says so."""
    without = tuple(arg for arg in OIDC_ARGS if "external-secrets.io" not in arg and arg != "--api-versions")
    argv = ["helm", "template", "rask", str(REPO / "chart"), *without, *DEFAULT_ARGS]
    refused = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603, S607

    assert refused.returncode != 0
    assert "the External Secrets Operator is a prerequisite: this cluster does not serve external-secrets.io/v1" in refused.stderr
