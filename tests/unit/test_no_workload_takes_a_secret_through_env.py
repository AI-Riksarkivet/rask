"""No workload in the chart takes a secret through its environment ([[XC-081]]).

The estate's rule (owner, verbatim): *"Never secret through envs. Either from ESO, secret store dapr and STS for zero
trust."* Two shapes break it in a pod spec. A `secretKeyRef` env entry injects a Kubernetes Secret into the process
environment: ESO syncing the value from the secret store fixes where it comes from, not how it is delivered. A literal
env `value:` holding minted material is worse, since the material then sits in the rendered manifest, the release
Secret and every GitOps diff. A secret reaches a pod as a mounted file, through the secret store, or not at all (a
store identity is a session its ServiceAccount token is exchanged for, ADR 0170).

Read off the render rather than a cluster, under the three overlays a release is made from: the chart's defaults, the
local-development overlay that carries every development fixture (the AGE Postgres, the bundled OpenFGA and its migrate
Job), and production.
"""

from __future__ import annotations

import re

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, OBSERVABILITY_ARGS, PROD_ARGS, REPO, pod_template, render


#: The two shapes this chart could mint secret material in: `sha256sum | trunc 40` (hex) and `randAlphaNum 40`. Matching
#: the value rather than the variable's name keeps `LINEAGE_SERVICE_TOKEN_FILE=/etc/...` (a sanctioned path) out.
_MINTED_MATERIAL = re.compile(r"^(?:[0-9a-f]{40}|[A-Za-z0-9]{40})$")

_OVERLAYS = {
    "default": (*DEFAULT_ARGS, "--set", "dapr.enabled=true", *OBSERVABILITY_ARGS),
    "local": ("--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml")),
    "production": PROD_ARGS,
}


def _secrets_in_env(docs: tuple[dict, ...]) -> list[str]:
    """Every pod env entry that carries a secret, as `<workload>/<container>: <var>`."""
    found: list[str] = []
    for doc in docs:
        template = pod_template(doc)
        if not template:
            continue
        spec = template.get("spec") or {}
        for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
            for env in container.get("env") or []:
                from_secret = "secretKeyRef" in (env.get("valueFrom") or {})
                value = env.get("value")
                minted = isinstance(value, str) and bool(_MINTED_MATERIAL.match(value))
                if from_secret or minted:
                    found.append(f"{doc['metadata']['name']}/{container['name']}: {env['name']}")
    return found


@pytest.mark.parametrize("overlay", list(_OVERLAYS))
def test_no_workload_takes_a_secret_through_env(overlay: str) -> None:
    """Under each overlay, no container or init container is handed a secret as an env var."""
    assert _secrets_in_env(render(*_OVERLAYS[overlay])) == []
