"""The annotator's release authenticates every request when `auth.enabled` is on.

The annotator is a client of the lakehouse with its own release (deploy/fixtures/annotator, ADR 0171), and every
decision it makes is the catalog's answer for the person's own token. Its checker is deliberately open with
authentication off (dev parity), so a release that dropped the OIDC env would serve every project and task to anyone,
while the catalog behind it stayed governed. Render-pinned on the release's own chart.
"""

from __future__ import annotations

from tests.unit.chart_render import ANNOTATOR_FIXTURE_ARGS, render_fixture


def _annotator_env(*extra: str) -> dict[str, str]:
    docs = render_fixture("annotator", *ANNOTATOR_FIXTURE_ARGS, *extra)
    [deployment] = [d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == "rask-annotator"]
    [container] = [c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "annotator"]
    return {e["name"]: e.get("value", "") for e in container.get("env", [])}


def test_the_annotator_authenticates_every_request_when_auth_is_on() -> None:
    env = _annotator_env("--set", "auth.enabled=true")
    expected = {"RASK_OIDC_ENABLED": "true", "RASK_OIDC_ISSUER": "https://auth.example.com/dex", "RASK_OIDC_AUDIENCE": "rask-lakehouse"}
    assert {name: env.get(name) for name in expected} == expected
    assert "RASK_INSECURE_ALLOW_UNAUTHENTICATED" not in env, "an authenticating annotator also allows the unauthenticated"
