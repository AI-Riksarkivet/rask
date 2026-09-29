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

from chart_yaml import FAST_LOADER


#: Services the gateway carries to the public edge. Each must be able to authenticate.
#: `compute` serves `{prefix}/ray` + `/api/serve`; `controlplane` serves `{prefix}/projects`.
PUBLICLY_PROXIED = ("compute", "controlplane")


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

    docs = [d for d in yaml.load_all(_helm_template("auth.enabled=true"), Loader=FAST_LOADER) if isinstance(d, dict)]
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
