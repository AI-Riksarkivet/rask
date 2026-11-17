"""Every governed door trusts the one IdP the deployment names ([[XC-148]]).

`auth.oidc.issuer` is the trust anchor: a governed service accepts a token only when its `iss` equals it. The development
Dex's in-cluster address (`http://<release>-dex`), and the escape hatch that lets a verifier fetch discovery and keys over
http, belong to `dex.enabled`, which a real deployment refuses. Without the development Dex no object in the render names
it, so a workload answering to the name `rask-dex` is sent no token exchange and no credential, and an issuer that is not
https is refused at render.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.chart.chart_render import DEFAULT_ARGS, PROD_ARGS, containers, env_of, render, render_text


_PLATFORM_IDP = "https://idp.platform.example/realms/rask"
_DEV_ISSUER = "http://localhost:8080/dex"

#: The overlay, the issuer it names, and what every governed door then carries: (issuer, allow-insecure, discovery URL).
_OVERLAYS = {
    "prod": ((*PROD_ARGS, "--set-string", f"auth.oidc.issuer={_PLATFORM_IDP}"), (_PLATFORM_IDP, "false", None)),
    "dev-dex": ((*DEFAULT_ARGS, "--set-string", f"auth.oidc.issuer={_DEV_ISSUER}"), (_DEV_ISSUER, "true", "http://rask-dex:5556/dex")),
}


@pytest.mark.parametrize("overlay", sorted(_OVERLAYS))
def test_every_door_trusts_the_named_issuer_and_only_the_dev_dex_is_reached_over_http(overlay: str) -> None:
    args, door = _OVERLAYS[overlay]
    docs = render(*args)
    found = [(f"{workload}/{name}", env_of(container)) for workload, name, container in containers(docs)]

    doors = {
        where: (env["RASK_OIDC_ISSUER"], env.get("RASK_OIDC_ALLOW_INSECURE"), env.get("RASK_OIDC_DISCOVERY_URL"))
        for where, env in found
        if env.get("RASK_OIDC_ENABLED") == "true"
    }
    assert {"Deployment/rask-catalog/catalog", "Deployment/rask-lineage/lineage"} <= doors.keys(), f"{overlay}: no governed door rendered: {sorted(doors)}"
    assert set(doors.values()) == {door}, f"{overlay}: a door trusts something other than the named issuer: {doors}"

    if door[2] is None:
        dev_dex = sorted(f"{doc['kind']}/{doc['metadata']['name']}" for doc in docs if "rask-dex" in json.dumps(doc))
        assert not dev_dex, f"{overlay}: without the development Dex the render still sends something to it: {dev_dex}"


_PARTNER = "https://partner.example.com"


@pytest.mark.parametrize(
    ("values", "refusal"),
    [
        pytest.param(
            ("--set", "dex.enabled=false", "--set-string", "auth.oidc.issuer=http://idp.platform.example"),
            "auth.oidc.issuer=http://idp.platform.example is not https",
            id="an-issuer-over-http-without-the-development-dex",
        ),
        pytest.param(
            ("--set-json", f'auth.oidc.providers=[{{"issuer":"{_PARTNER}","idpId":"dex"}}]'), "names the idp id dex, which", id="two-issuers-name-one-idp-id"
        ),
        pytest.param(("--set-json", f'auth.oidc.providers=[{{"issuer":"{_DEV_ISSUER}","idpId":"partner"}}]'), "is not https", id="a-further-issuer-over-http"),
        pytest.param(
            ("--set-json", f'auth.oidc.providers=[{{"issuer":"{_PARTNER}","idpId":"partner"}},{{"issuer":"{_PARTNER}/","idpId":"other"}}]'),
            "named as an issuer twice",
            id="one-further-issuer-named-twice",
        ),
        pytest.param(
            ("--set-json", f'auth.oidc.providers=[{{"issuer":"{_PARTNER}","idpId":"partner","clientMarker":{{"claim":"sub"}}}}]'),
            "never one every token carries",
            id="a-client-marker-every-token-carries",
        ),
    ],
)
def test_an_issuer_the_doors_could_not_name_callers_by_is_refused_at_render(values: tuple[str, ...], refusal: str) -> None:
    """[[XC-148]], ADR 0208: an issuer reached over http, named twice, sharing an idp id with another issuer, or marking its
    clients with a claim every token carries is refused before any pod starts, rather than crash-looping one at boot."""
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_text(*DEFAULT_ARGS, "--set-string", f"auth.oidc.issuer={_DEV_ISSUER}", *values)

    assert refusal in refused.value.stderr
