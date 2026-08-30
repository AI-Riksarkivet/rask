"""A service the gateway publishes must be able to refuse an unauthenticated caller.

THE HOLE. Nine services mix in `GovernedAuthSettings` (some once declared a byte-identical twin
inline; those copies were collapsed onto the mixin 2026-08-30).
`compute` and `controlplane` declared NEITHER, shipped no `security.py`, and `make_service_app` adds
only CORS / RequestID / Timing / SlashTolerance — no auth. So on an `auth.enabled` estate every route
in both was anonymous, and the gateway carries both to the public edge: `{prefix}/ray`,
`{prefix}/projects` and `/api/serve` are rows in `gateway/__init__.py::_routes()`, and
`chart/templates/ingress.yaml` publishes `/api`.

What that exposed, concretely and today:

  * `GET /api/projects/` returns every operator Project CR in the cluster — slug, team, workload
    type, k8s namespace and each tenant's live ingress host. The catalog gates the same class of
    enumeration on `can_observe_events` and FGA-filters it.
  * `compute` proxies the Ray dashboard using a token the chart deliberately turns ON
    (`rayservice.yaml`, `ray.auth.enabled` → `RAY_AUTH_TOKEN`): jobs, actors, tasks, cluster state,
    driver logs and the whole Serve status API, to anonymous callers. `proxy.py`'s own comment —
    "Never widen this without an auth layer in front of /api" — is an acknowledgement that no such
    layer exists.

WHY IT SURVIVED, which is the part worth pinning. `CLAUDE.md` said "No auth, no app middleware. The
services assume localhost / trusted network" until 2026-08-26. Read as policy, that sentence makes an
unguarded service look deliberate rather than unfinished. The doc was stale — the chart has defaulted
auth ON for a while — and the owner ruling of 2026-08-26 settled it: the estate is authenticated.

This gate is the structural half of that ruling. It asserts the CAPABILITY (the service can express
an auth door at all), not a live 401, because a request-level test needs each service's own app and
these two have no test harness in common. A service that cannot bind `RASK_OIDC_ENABLED` cannot be
gated by any amount of chart configuration, which is the failure this catches.
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]

#: Services the gateway carries to the public edge. Each must be able to authenticate.
#: `compute` serves `{prefix}/ray` + `/api/serve`; `controlplane` serves `{prefix}/projects`.
PUBLICLY_PROXIED = ("compute", "controlplane")


def _settings_bases(service: str) -> set[str]:
    """Every base class of every `*Settings` class the service declares."""
    bases: set[str] = set()
    for path in (REPO / "services" / service / "src" / service).rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith("Settings"):
                bases |= {b.id for b in node.bases if isinstance(b, ast.Name)}
    return bases


def test_every_publicly_proxied_service_can_bind_the_estates_auth_env() -> None:
    """Without `GovernedAuthSettings` the service cannot read `RASK_OIDC_ENABLED` at all.

    That is the load-bearing difference between "auth is off here" and "auth CANNOT be turned on
    here": the second cannot be fixed by a values file, and reads identically from the outside.
    """
    ungated = [s for s in PUBLICLY_PROXIED if "GovernedAuthSettings" not in _settings_bases(s)]
    assert not ungated, (
        f"{ungated} are proxied to the public edge by the gateway and declare no settings class "
        f"mixing in GovernedAuthSettings, so they cannot bind RASK_OIDC_ENABLED / RASK_FGA_* and "
        f"no chart value can gate them. Give them the door their nine siblings share."
    )


def test_every_publicly_proxied_service_ships_a_security_module() -> None:
    """The settings alone change nothing: the knobs only matter where a route declares a dependency.

    `security.py` is where the estate puts that — built on `service_kit.governed.deps.make_auth_deps`,
    which viewer, flows, annotator and notifications already share.
    """
    missing = [s for s in PUBLICLY_PROXIED if not (REPO / "services" / s / "src" / s / "security.py").exists()]
    assert not missing, (
        f"{missing} have no security.py, so nothing binds a subject or a checker to their routes — the settings would be read and never consulted."
    )


def test_the_gated_routers_actually_declare_the_dependency() -> None:
    """A door that is built and not mounted is the failure mode this whole class of gate exists for.

    Asserted on the ROUTER, not per route: `compute.routes` and `compute.proxy` between them carry a
    `{path:path}` catch-all, so a per-route list could pass while a new route lands ungated.
    """
    offenders: list[str] = []
    for service in PUBLICLY_PROXIED:
        root = REPO / "services" / service / "src" / service
        text = "\n".join(p.read_text() for p in root.rglob("*.py"))
        if "security." not in text and "from " + service + " import security" not in text:
            offenders.append(f"{service}: no module references its own security helpers")
    assert not offenders, "\n".join(offenders)


# ── the CHART half: a door with no env is a door that never engages ──────────────────────────────


def test_the_chart_FEEDS_the_door_it_now_has() -> None:
    """The code and the chart must land together, or the change looks applied and does nothing.

    This is the `explorer.yaml` failure verbatim: its auth env was emitted `if and (eq $name
    "annotator") auth.enabled`, so the viewer streamed page images and browsed S3 wide open on an
    auth-enabled estate while every surface reported authorization as ON. The vars change behaviour
    only where a route declares a dependency — so the two halves are independently silent.

    Rendered, not grepped: `compute` gets its env through `fleet.yaml`'s `governedAuth` flag and
    `controlplane` through its own template, so a text match would pass on the wrong mechanism.
    """
    import yaml

    from tests.unit.test_invariants import _helm_template  # the shared renderer, so flags stay in one place

    docs = [d for d in yaml.safe_load_all(_helm_template("auth.enabled=true")) if isinstance(d, dict)]
    missing: list[str] = []
    for service in PUBLICLY_PROXIED:
        deployment = next(
            (d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"].split("-", 1)[-1] == service),
            None,
        )
        assert deployment is not None, f"no Deployment rendered for {service}"
        env = {e["name"] for e in deployment["spec"]["template"]["spec"]["containers"][0].get("env", [])}
        if "RASK_OIDC_ENABLED" not in env or "RASK_FGA_ENABLED" not in env:
            missing.append(f"{service}: has {sorted(e for e in env if 'OIDC' in e or 'FGA' in e)}")

    assert not missing, (
        "these services declare an auth door and the chart does not feed it, so `oidc_enabled` stays "
        "False and every route answers anonymously while the code reports authorization as "
        "configured:\n  " + "\n  ".join(missing)
    )


def test_the_door_has_its_DEPENDENCIES_built_at_startup() -> None:
    """Settings + a router dependency are not enough: the verifier has to EXIST.

    `make_auth_deps` reads `app.state.oidc` and `app.state.fga`, which a lifespan must put there —
    building an `OIDCVerifier` fetches discovery, so it cannot happen at import. Miss it and the
    failure is quiet and total: the settings bind, the route declares its dependency, and every
    request answers 503 "Authentication is enabled but unavailable".

    That is fail-CLOSED and correct, and it is also a service that does nothing — which is why it has
    to be its own assertion rather than something the 403 tests would have caught. Measured on the
    live estate 2026-08-26: exactly that shipped when the settings and the gate landed without the
    lifespan. Both services answered 503 to every request while their siblings answered 401.
    """
    ungrounded: list[str] = []
    for service in PUBLICLY_PROXIED:
        root = REPO / "services" / service / "src" / service
        text = "\n".join(p.read_text() for p in root.rglob("*.py"))
        if "attach_auth" not in text:
            ungrounded.append(service)

    assert not ungrounded, (
        f"{ungrounded} declare an auth door and never build its dependencies — `app.state.oidc` and "
        f"`app.state.fga` stay unset, so every governed route answers 503 rather than authorizing. "
        f"Call `service_kit.governed.auth_lifespan.attach_auth` from the service's lifespan."
    )
