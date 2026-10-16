"""Every governed door and every zone trusts the one IdP the deployment names ([[XC-148]]).

`auth.oidc.issuer` is the trust anchor: a governed service accepts a token only when its `iss` equals it, and the zones
sign in at it. The development Dex's in-cluster address (`http://<release>-dex`), and the escape hatch that lets a
verifier fetch discovery and keys over http, belong to `dex.enabled`, which a real deployment refuses. Without the
development Dex no object in the render names it, so a workload answering to the name `rask-dex` is sent no browser, no
token exchange and no credential, and an issuer that is not https is refused at render.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, PROD_ARGS, containers, env_of, render, render_text


_PLATFORM_IDP = "https://idp.platform.example/realms/rask"
_DEV_ISSUER = "http://localhost:8080/dex"

#: The overlay, the issuer it names, and what every governed door then carries: (issuer, allow-insecure, discovery URL).
_OVERLAYS = {
    "prod": ((*PROD_ARGS, "--set-string", f"auth.oidc.issuer={_PLATFORM_IDP}", "--set", "explorer.enabled=true"), (_PLATFORM_IDP, "false", None)),
    "dev-dex": ((*DEFAULT_ARGS, "--set-string", f"auth.oidc.issuer={_DEV_ISSUER}"), (_DEV_ISSUER, "true", "http://rask-dex:5556/dex")),
}


@pytest.mark.parametrize("overlay", sorted(_OVERLAYS))
def test_every_door_and_zone_trusts_the_named_issuer_and_only_the_dev_dex_is_reached_over_http(overlay: str) -> None:
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

    zones = {where: env["OIDC_ISSUER"] for where, env in found if "OIDC_ISSUER" in env}
    assert zones, f"{overlay}: no zone signs in, so the zone half would pass vacuously"
    assert set(zones.values()) == {door[0]}, f"{overlay}: a zone signs in at another issuer: {zones}"

    if door[2] is None:
        dev_dex = sorted(f"{doc['kind']}/{doc['metadata']['name']}" for doc in docs if "rask-dex" in json.dumps(doc))
        assert not dev_dex, f"{overlay}: without the development Dex the render still sends something to it: {dev_dex}"


def test_an_issuer_that_is_not_https_is_refused_without_the_development_dex() -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_text(*DEFAULT_ARGS, "--set", "dex.enabled=false", "--set-string", "auth.oidc.issuer=http://idp.platform.example")

    assert "auth.oidc.issuer=http://idp.platform.example is not https" in refused.value.stderr
