"""The console's `/config.json` asks the lakehouse's issuer, as a public client its Dex declares, for a token the governed doors accept.

The console is the lakehouse's own single-page app with no backend: the browser reads `/config.json` at boot and signs in
with PKCE as the client it names (`frontend/console/src/lib/auth.svelte.ts`). The catalog accepts a token only when its
`iss` is `auth.oidc.issuer` and its `aud` carries the lakehouse's audience, which Dex issues only for the cross-client
scope. A person is the principal the issuer names ([[XC-214]]): by `sub` under `auth.oidc.idpId`, which any OpenID
provider issues, or, where `auth.oidc.connectors` names a Dex's connectors, by the `federated_claims` Dex adds only for
`federated:id`, so the console asks for that scope then and never otherwise. A client the dev Dex
does not declare public could not complete the exchange without a secret the browser does not hold, and the Vite dev
server (`make dev`, :5173) is an origin of its own, whose discovery, key and token reads at Dex are cross-origin.

Read from the product chart's render and the dex fixture's, as `make k3s-up` installs them: the chart adds the scopes
the doors require to whatever `console.oidc.scopes` names, so a value cannot drop them.
"""

from __future__ import annotations

import json
from typing import Final

import pytest
import yaml

from tests.unit.chart_render import containers, env_of, render, render_fixture


_ISSUER: Final = "https://idp.example.com/dex"


@pytest.mark.parametrize(
    ("naming", "federated", "person_env"),
    [
        pytest.param((), ["federated:id"], {"RASK_OIDC_CONNECTORS": '{"local":"dex"}'}, id="a-dex-named-connector-by-connector"),
        pytest.param(("--set-string", "auth.oidc.idpId=corp"), [], {"RASK_OIDC_IDP_ID": "corp"}, id="any-openid-provider-named-by-its-idp-id"),
    ],
)
def test_the_console_config_names_the_issuer_a_public_client_and_the_scopes_the_lakehouse_doors_require(
    naming: tuple[str, ...], federated: list[str], person_env: dict[str, str]
) -> None:
    lakehouse = render(
        "--set", "image.localImages=true", "--set-string", f"auth.oidc.issuer={_ISSUER}", "--set-json", 'console.oidc.scopes=["openid"]', *naming
    )
    dex = render_fixture("dex")

    [config] = [
        json.loads(doc["data"]["config.json"]) for doc in lakehouse if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "rask-console-config"
    ]
    [catalog_env] = [env_of(container) for workload, name, container in containers(lakehouse) if workload == "Deployment/rask-catalog" and name == "catalog"]
    audience = catalog_env["RASK_OIDC_AUDIENCE"]
    naming_env = {key: value for key, value in catalog_env.items() if key in {"RASK_OIDC_IDP_ID", "RASK_OIDC_SUBJECT_CLAIMS", "RASK_OIDC_CONNECTORS"}}
    [dex_config] = [yaml.safe_load(doc["data"]["config.yaml"]) for doc in dex if doc.get("kind") == "ConfigMap"]
    declared = {client["id"]: client for client in dex_config["staticClients"]}

    assert (config["issuer"], config["clientId"]) == (_ISSUER, "rask-console")
    assert (config["scopes"], naming_env) == (["openid", *federated, f"audience:server:client_id:{audience}"], person_env)
    assert (declared[config["clientId"]].get("public"), "secret" in declared[config["clientId"]]) == (True, False)
    assert dex_config["web"].get("allowedOrigins") == ["http://localhost:5173"]
