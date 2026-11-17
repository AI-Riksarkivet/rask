"""No credential is minted by, carried in, or accepted into the chart's render ([[XC-004]]).

A render is stored whole in the release (`helm get manifest`, `hooks`, `values`), every revision of it, so
a credential that passes through one is a secret at rest outside the store. A credential could pass through
three ways: a template minting it (`randAlphaNum`), a value carrying it (`age.password`,
`objectStore.secretKey`, ...), and a template rendering it into a Secret or a Job.
The credentials live in the store and reach pods through ESO, a prerequisite the chart checks for.
The dev store's are generated in-cluster; `test_the_dev_openbao_is_seeded_by_its_own_pod.py` runs that half.

The render tests of `templates/prod-credentials.yaml` live here, and that template also refuses a real deployment
(`rask.isRealDeployment`) whatever would expose it once installed ([[XC-184]]): a published dev credential,
authentication off, and a platform address a credential or the estate's data would cross over http.
"""

from __future__ import annotations

import subprocess

import pytest
import yaml

from tests.unit.chart_render import DEFAULT_ARGS, OIDC_ARGS, PROD_ARGS, REPO, render_chart_text, render_text
from tests.unit.chart_yaml import FAST_LOADER


#: The two cluster-less renders an operator makes: the chart's dev defaults and the local estate's overlay, each with
#: the dev store's credentials generated, which is where a render-time mint would sit.
_RENDERS = {
    "dev": DEFAULT_ARGS,
    "local": ("--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml")),
}


@pytest.mark.parametrize("overlay", sorted(_RENDERS))
def test_a_cluster_less_render_mints_no_credential(overlay: str) -> None:
    """Two renders of one overlay are byte-identical, so nothing in them was generated at render time."""
    first = render_chart_text(REPO / "chart", *_RENDERS[overlay])
    second = render_chart_text(REPO / "chart", *_RENDERS[overlay])

    generators = {doc["metadata"]["name"] for doc in yaml.load_all(first, Loader=FAST_LOADER) if isinstance(doc, dict) and doc.get("kind") == "Password"}
    assert generators == {"rask-dev-credential-object-store-secret-key", "rask-dev-credential-postgres-password"}, (
        f"{overlay}: the dev credentials are not generated, so this would pass vacuously"
    )
    assert first == second, f"{overlay}: two renders differ, so the render minted something the release then stores"


def test_a_credential_passed_as_a_value_is_refused_naming_it() -> None:
    """A credential passed as a value is refused, by name, on any render: no credential is a key `values.yaml` declares, and
    the schema refuses a key it does not declare at every level, before anything is stored in the release."""
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_text(*DEFAULT_ARGS, "--set-string", "age.password=a-password", "--set-string", "objectStore.secretKey=a-root-secret")

    assert sorted(line for line in refused.value.stderr.splitlines() if "additional properties" in line) == [
        "- at '/age': additional properties 'password' not allowed",
        "- at '/objectStore': additional properties 'secretKey' not allowed",
    ]


@pytest.mark.parametrize(
    ("setting", "reason"),
    [
        pytest.param(("--set", "dex.enabled=true"), "dex.enabled=true — the bundled dev IdP", id="the-bundled-dev-idp"),
        pytest.param(("--set", "auth.enabled=false"), "auth.enabled=false — every governed door answers anyone", id="authentication-off"),
        pytest.param(
            ("--set-string", "image.tags.web-console=dev"),
            "ghcr.io/example/rask/web-console:dev — a first-party image at the development tag",
            id="an-image-at-the-development-tag",
        ),
        pytest.param(
            ("--set-string", "auth.fgaApiUrl=http://fga.example.com"),
            "auth.fgaApiUrl=http://fga.example.com is not https",
            id="the-platforms-openfga-over-http",
        ),
        pytest.param(
            ("--set-string", "objectStore.externalEndpoint=http://s3.example.com"),
            "objectStore.externalEndpoint=http://s3.example.com is not https",
            id="the-platforms-store-over-http",
        ),
        pytest.param(
            ("--set-string", "vending.endpoint=http://s3.example.com"), "vending.endpoint=http://s3.example.com is not https", id="the-vended-store-over-http"
        ),
        pytest.param(
            ("--set-string", "observability.export.endpoint=http://otlp.example.com:4318/v1/otlp"),
            "observability.export.endpoint=http://otlp.example.com:4318/v1/otlp is not https",
            id="the-platforms-telemetry-backend-over-http",
        ),
        pytest.param(
            ("--set-string", "services.lineage.forward.url=http://graph.example.com"),
            "services.lineage.forward.url=http://graph.example.com is not https",
            id="the-platforms-graph-backend-over-http",
        ),
        pytest.param(
            ("--set-string", "openbao.externalAddr=http://vault.example.com:8200"),
            'openbao.externalAddr is "http://vault.example.com:8200": every service sends its ServiceAccount token',
            id="the-platforms-secret-store-over-http",
        ),
        pytest.param(
            ("--set-string", "nats.url=nats://bus.example.com:4222"),
            "nats.url names nats://bus.example.com:4222, which is not tls:// — every service and the stream Job present their ServiceAccount token",
            id="the-platforms-bus-in-the-clear",
        ),
        pytest.param(
            ("--set-string", "nats.url=tls://a.example.com:4222\\,nats://b.example.com:4222"),
            "nats.url names nats://b.example.com:4222, which is not tls://",
            id="one-server-of-the-bus-in-the-clear",
        ),
    ],
)
def test_a_real_deployment_refuses_what_would_expose_it(setting: tuple[str, ...], reason: str) -> None:
    """The production overlay with one setting that would expose the estate once installed is refused, naming it.

    The bundled Dex's static users and client secret are published in this repository, so it is a development render's
    alone. Each pod presents its ServiceAccount token to the bus in CONNECT, so a bus server named in the clear would hand
    it to whoever reads the path. Authentication off makes the chart write the acknowledgement the services refuse to boot without, so on a real
    deployment nobody would have said so.
    """
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_text(*PROD_ARGS, *setting)

    assert reason in refused.value.stderr


#: The AGE-on-CNPG path, the one part of the render that needs the CloudNativePG API ([[XC-130]]).
_CNPG_CLUSTER = (
    "--set",
    "age.enabled=false",
    "--set",
    "age.cnpgCluster.enabled=true",
    "--set-string",
    f"age.cnpgCluster.extensionImage=reg.example/age@sha256:{'ab' * 32}",
)


@pytest.mark.parametrize(
    ("api", "message", "enable"),
    [
        ("external-secrets.io", "the External Secrets Operator is a prerequisite: this cluster does not serve external-secrets.io/v1", ()),
        ("nats.url", "NATS JetStream is a prerequisite: set nats.url", ()),
        (
            "postgresql.cnpg.io",
            "CloudNativePG is a prerequisite: age.cnpgCluster.enabled is on and this cluster does not serve postgresql.cnpg.io/v1",
            _CNPG_CLUSTER,
        ),
    ],
    ids=["external-secrets", "nats", "cloudnative-pg"],
)
def test_a_render_for_a_cluster_without_a_prerequisite_fails_naming_it(api: str, message: str, enable: tuple[str, ...]) -> None:
    """ESO, the NATS bus and, for its Cluster, the CloudNativePG operator are prerequisites rask never installs
    (ADR 0153): a render for a cluster that does not serve one, or that names no bus, stops and says which."""
    pairs = zip(OIDC_ARGS[::2], OIDC_ARGS[1::2], strict=True)
    without = tuple(arg for flag, value in pairs if api not in value for arg in (flag, value))
    argv = ["helm", "template", "rask", str(REPO / "chart"), *without, *DEFAULT_ARGS, *enable]
    refused = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603, S607

    assert refused.returncode != 0
    assert message in refused.stderr
