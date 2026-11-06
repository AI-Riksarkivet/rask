"""The inbox's sidecar verifies the Postgres it keeps its actors' rows on, the bundled Cluster or a platform's, before it sends the password.

Platform services are named, never bundled: `state.external.connectionSecret` names a platform's Postgres, and the
release then renders no Cluster and reads that Secret alone. Either way the sidecar connects with
`PGSSLMODE=verify-full`: against the CA it is given (the bundled Cluster's `-ca`, or `state.external.caSecret`), or
against its system roots, so a connection string that names no `sslmode` never sends the password to a server nothing
verified. Under rask's default-deny the platform's server is outside the namespace, so its egress must be named.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Final

import pytest


_VERIFIED_BY_CA: Final = "PGSSLMODE=verify-full,PGSSLROOTCERT=/var/run/secrets/notifications/state-ca/ca.crt"


_PLATFORM: Final = ("--set", "state.external.connectionSecret=pg-conn")


@pytest.mark.parametrize(
    ("values", "cluster", "credential", "sslenv", "ca"),
    [
        pytest.param(
            (), ["rask-notifications-state"], "rask-notifications-state-app", _VERIFIED_BY_CA, "rask-notifications-state-ca", id="the-bundled-cluster"
        ),
        pytest.param((*_PLATFORM, "--set", "state.external.caSecret=pg-ca"), [], "pg-conn", _VERIFIED_BY_CA, "pg-ca", id="a-platform-postgres-and-its-ca"),
        pytest.param(_PLATFORM, [], "pg-conn", "PGSSLMODE=verify-full", None, id="a-platform-postgres-on-the-system-roots"),
    ],
)
def test_the_inbox_keeps_its_state_on_the_postgres_it_names_and_verifies_it(
    render_console: Callable[..., tuple[dict, ...]], values: tuple[str, ...], cluster: list[str], credential: str, sslenv: str, ca: str | None
) -> None:
    release = render_console(*values)

    [inbox] = [doc for doc in release if doc.get("kind") == "Deployment" and doc["spec"]["template"]["metadata"]["annotations"].get("dapr.io/app-id")]
    annotations = inbox["spec"]["template"]["metadata"]["annotations"]
    [store] = [doc for doc in release if doc.get("kind") == "Component" and doc["spec"]["type"] == "state.postgresql"]
    [reader] = [doc for doc in release if doc.get("kind") == "Role" and doc["metadata"]["name"].endswith("-state-credential")]
    volumes = {volume["name"]: volume for volume in inbox["spec"]["template"]["spec"]["volumes"]}

    assert [doc["metadata"]["name"] for doc in release if doc.get("kind") == "Cluster"] == cluster
    assert ({entry["secretKeyRef"]["name"] for entry in store["spec"]["metadata"] if "secretKeyRef" in entry}, reader["rules"][0]["resourceNames"]) == (
        {credential},
        [credential],
    )
    assert annotations.get("dapr.io/env") == sslenv
    assert (volumes.get("state-ca", {}).get("secret", {}).get("secretName"), "state-ca:" in annotations["dapr.io/volume-mounts"]) == (ca, ca is not None)


def test_the_inbox_on_a_platform_postgres_is_refused_without_the_egress_reaching_it(render_console: Callable[..., tuple[dict, ...]]) -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_console(
            "--set", "state.external.connectionSecret=pg-conn",
            "--set", "networkPolicy.enabled=true",
            "--set-json", 'networkPolicy.apiServerEgress=[{"to":[{"ipBlock":{"cidr":"10.43.0.1/32"}}]}]',
        )  # fmt: skip
    assert "state.external.egress must name the external Postgres the inbox's sidecar connects to" in refused.value.stderr
