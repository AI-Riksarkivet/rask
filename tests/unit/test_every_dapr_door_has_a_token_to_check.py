"""A deployment whose code guards a Dapr door must be given something to check the door against.

[[LH-162]]. `require_dapr_token` compares the sidecar's `dapr-api-token` header against the app's own
app-API token. `dapr.io/app-token-secret` gives the SIDECAR that token; nothing in the chart guaranteed
the APP was told it too. `rask-annotator` was the case: it guards `/api/jobs/apply` and the whole
`DaprActor` callback surface, its sidecar stamped a valid header, and the container had no token to
compare against — so every guarded route admitted every caller. Measured on the running pod
2026-09-15, `GET /dapr/config` answered 200 with no token presented.

WHY NOTHING CAUGHT IT. The code half was tested (the door refuses a wrong token) and the chart half was
tested (the Secret renders, the annotation is present). The two halves were never joined, so a service
could satisfy both while holding neither end of the same string. That is the identical split
[[LH-160]]'s ratchet was written for, one layer down.

THE PACKAGES ARE NAMED. `_GUARDS_A_DAPR_DOOR` lists the service packages that mount a route behind
`require_dapr_token`, and a service that starts guarding a door joins it in the same change. The set
is checked against the render: a named package with no Deployment fails, so the
list cannot rot into naming nothing.
"""

from __future__ import annotations

from typing import Any

import yaml

from tests.unit.test_invariants import _helm_template


#: The service packages that mount a route guarded by `require_dapr_token`.
_GUARDS_A_DAPR_DOOR = frozenset({"catalog", "lineage", "maintenance", "notifications"})

#: Any ONE of these gives the app an answer to "what token should I expect?". The third is a refusal to
#: answer that was made deliberately — it still counts, because the point of the gate is that the choice
#: is visible in the render, not that every door is closed.
_TOKEN_SOURCES = ("APP_API_TOKEN", "RASK_APP_TOKEN_FROM_STORE", "RASK_ALLOW_UNAUTHENTICATED_DAPR")


def _deployed_apps() -> list[tuple[str, str, set[str]]]:
    """Every sidecar-bearing Deployment as (app-id, python package, env var names).

    The package comes from the uvicorn target (`annotator.main:app` -> `annotator`), which is the same
    string the image actually runs — so this cannot drift from what is deployed the way a mapping
    table would.
    """
    raw = _helm_template("dapr.enabled=true")
    apps: list[tuple[str, str, set[str]]] = []
    for doc in yaml.safe_load_all(raw):
        if not doc or doc.get("kind") != "Deployment":
            continue
        template = doc["spec"]["template"]
        annotations: dict[str, Any] = (template.get("metadata") or {}).get("annotations") or {}
        if annotations.get("dapr.io/enabled") != "true":
            continue
        for container in (template.get("spec") or {}).get("containers", []) or []:
            target = next((arg for arg in (container.get("args") or []) if ":" in arg and not arg.startswith("-")), "")
            if not target:
                continue
            apps.append(
                (
                    annotations.get("dapr.io/app-id", doc["metadata"]["name"]),
                    target.split(":")[0].split(".")[0],
                    {e["name"] for e in (container.get("env") or [])},
                )
            )
    return apps


def test_every_deployment_that_guards_a_dapr_door_is_told_what_to_expect() -> None:
    apps = _deployed_apps()
    assert {package for _, package, _ in apps} >= _GUARDS_A_DAPR_DOOR, "a package named as guarding a Dapr door renders no Deployment"

    blind = [(app_id, package) for app_id, package, env in apps if package in _GUARDS_A_DAPR_DOOR and not (env & set(_TOKEN_SOURCES))]

    assert blind == [], (
        f"these deployments guard a Dapr door with nothing to check it against: {blind}. "
        f"Give the app one of {list(_TOKEN_SOURCES)} — RASK_APP_TOKEN_FROM_STORE is the sanctioned one "
        "(the secret store, not the environment); RASK_ALLOW_UNAUTHENTICATED_DAPR is a deliberate open door."
    )
