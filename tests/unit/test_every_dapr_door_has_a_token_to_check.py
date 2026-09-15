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

THE PAIRING IS DERIVED, NOT LISTED. The container's uvicorn target names its package, and whether that
package calls `require_dapr_token` is a fact in the source tree — so a new service that starts guarding
a door is covered the day it does, and one that stops is dropped the day it stops. A hand-maintained
list of "services that need a token" is the thing that would have been out of date here: the annotator
had been guarding a door since the jobs gate landed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tests.unit.test_invariants import _helm_template


_SERVICES = Path(__file__).resolve().parents[2] / "services"

#: Any ONE of these gives the app an answer to "what token should I expect?". The third is a refusal to
#: answer that was made deliberately — it still counts, because the point of the gate is that the choice
#: is visible in the render, not that every door is closed.
_TOKEN_SOURCES = ("APP_API_TOKEN", "RASK_APP_TOKEN_FROM_STORE", "RASK_ALLOW_UNAUTHENTICATED_DAPR")


def _packages_that_guard_a_dapr_door() -> frozenset[str]:
    """Service packages whose source calls `require_dapr_token` anywhere outside their tests."""
    guarding = set()
    for service in sorted(_SERVICES.iterdir()):
        src = service / "src"
        if not src.is_dir():
            continue
        if any("require_dapr_token" in path.read_text(encoding="utf-8") for path in src.rglob("*.py")):
            guarding.add(service.name)
    return frozenset(guarding)


def _deployed_apps() -> list[tuple[str, str, set[str]]]:
    """Every sidecar-bearing Deployment as (app-id, python package, env var names).

    The package comes from the uvicorn target (`annotator.main:app` -> `annotator`), which is the same
    string the image actually runs — so this cannot drift from what is deployed the way a mapping
    table would.
    """
    raw = _helm_template("dapr.enabled=true", "medallion.enabled=true", "explorer.enabled=true")
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
    guarding = _packages_that_guard_a_dapr_door()
    assert guarding, "no service calls require_dapr_token — the gate is reading the wrong tree"

    blind = [(app_id, package) for app_id, package, env in _deployed_apps() if package in guarding and not (env & set(_TOKEN_SOURCES))]

    assert blind == [], (
        f"these deployments guard a Dapr door with nothing to check it against: {blind}. "
        f"Give the app one of {list(_TOKEN_SOURCES)} — RASK_APP_TOKEN_FROM_STORE is the sanctioned one "
        "(the secret store, not the environment); RASK_ALLOW_UNAUTHENTICATED_DAPR is a deliberate open door."
    )


def test_the_gate_can_see_the_service_that_defected() -> None:
    """The annotator must be IN scope, or the test above passes by measuring nothing.

    It is named here rather than left implicit because the derivation has two ways to silently cover
    nothing — a package name that stops matching the uvicorn target, and a source scan that stops
    finding the call — and both would leave an empty list comparing equal to an empty list.
    """
    assert "annotator" in _packages_that_guard_a_dapr_door()
    assert any(package == "annotator" for _, package, _ in _deployed_apps()), "the annotator Deployment is not in the render this gate reads"
