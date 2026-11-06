"""The console's `/config.json` asks the lakehouse's issuer, as a public client its Dex declares, for a token the governed doors accept.

The console is a single-page app with no backend: the browser reads `/config.json` at boot and signs in with PKCE as the
client it names (`frontend/console/src/lib/auth.svelte.ts`). The catalog accepts a token only when its `iss` is
`auth.oidc.issuer`, its `aud` carries the lakehouse's audience, which Dex issues only for the cross-client scope, and it
carries `federated_claims`, from which every principal is derived (`federated:id`). A client the dev Dex does not declare
public could not complete the exchange without a secret the browser does not hold.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Final

import yaml

from tests.unit.chart_render import REPO, containers, env_of, render, render_fixture


_ISSUER: Final = "https://idp.example.com/dex"


def test_the_console_config_names_the_issuer_a_public_client_and_the_scopes_the_lakehouse_doors_require(
    render_console: Callable[..., tuple[dict, ...]],
) -> None:
    console = render_console("--set-string", f"auth.oidc.issuer={_ISSUER}")
    lakehouse = render("--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml"))
    dex = render_fixture("dex", "-f", str(REPO / "deploy/fixtures/console/dex-values.yaml"))

    [config] = [json.loads(doc["data"]["config.json"]) for doc in console if doc.get("kind") == "ConfigMap" and "config.json" in doc.get("data", {})]
    [audience] = {
        env_of(container)["RASK_OIDC_AUDIENCE"]
        for workload, name, container in containers(lakehouse)
        if workload == "Deployment/rask-catalog" and name == "catalog"
    }
    [dex_config] = [yaml.safe_load(doc["data"]["config.yaml"]) for doc in dex if doc.get("kind") == "ConfigMap"]
    declared = {client["id"]: client for client in dex_config["staticClients"]}

    assert (config["issuer"], config["clientId"]) == (_ISSUER, "rask-console")
    assert {"openid", "federated:id", f"audience:server:client_id:{audience}"} <= set(config["scopes"]), config["scopes"]
    assert (declared[config["clientId"]].get("public"), "secret" in declared[config["clientId"]]) == (True, False)
