"""The control lane's relay, end to end through the RENDER — a values key that renders nothing is the bug.

`catalog/api/control_relay.py` drains `LANCE_CONTROL_OUTBOX_URI` and re-publishes what a bus outage
left staged. Three independent artefacts have to agree for a single tick to happen, and any two of them
can agree while the third drifts, silently:

1. a `bindings.cron` Component scoped to the catalog app-id (no Component, no ticks);
2. the catalog container's `LANCE_CONTROL_RELAY_BINDING_NAME` (Dapr delivers an input binding to
   `POST /<component name>`, so a second literal here is a cron firing into a 404);
3. the route the app actually serves at its pod ROOT.

Plus the one that makes staging worth doing at all: the control prefix must not be the LINEAGE prefix.
Lineage's reconcile cron parses every object in its own prefix as an OpenLineage `RunEvent` and DELETES
what it cannot parse, so a shared prefix hands the control lane's only durable copy to something that
destroys it — strictly worse than staging nowhere.

Why this matters more than the usual wiring gate: `table_published` is the ONLY thing that wakes
silver->gold. Nothing re-reads the `published` tag, so a lost publish stops the cascade with the tag
advanced, the route 200 and every pod green.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest
import yaml


REPO = pathlib.Path(__file__).resolve().parents[2]
CHART = REPO / "chart"


def _render(*set_values: str) -> list[dict]:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not pathlib.Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", "rask", str(CHART), "--set", "image.localImages=true"]
    argv += ["--set-string", "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum"]
    argv += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    argv += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    for value in set_values:
        argv += ["--set", value]
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603
    return [doc for doc in yaml.safe_load_all(out) if isinstance(doc, dict)]


def _catalog_env(docs: list[dict]) -> dict[str, str]:
    """The catalog container's plain-valued env, keyed by name (selected by LABEL, not by release name)."""
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        labels = ((doc["spec"]["template"].get("metadata") or {}).get("labels")) or {}
        if labels.get("app.kubernetes.io/component") != "catalog":
            continue
        env: dict[str, str] = {}
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            for item in container.get("env") or []:
                if "value" in item:
                    env[item["name"]] = str(item["value"])
        return env
    return {}


def _relay_component(docs: list[dict]) -> dict:
    for doc in docs:
        if doc.get("kind") != "Component" or (doc.get("spec") or {}).get("type") != "bindings.cron":
            continue
        if "catalog" in (doc.get("scopes") or []):
            return doc
    raise AssertionError(
        "no bindings.cron Component is scoped to `catalog` — nothing ever drains the control outbox, so a staged table_published is never re-published"
    )


def test_the_catalog_stages_control_events_into_a_prefix_its_OWN_relay_drains() -> None:
    """Staged, and staged somewhere the control relay reads — never the lineage prefix."""
    env = _catalog_env(_render())
    assert env, "no catalog Deployment rendered"

    staged = env.get("LANCE_CONTROL_OUTBOX_URI", "")
    assert staged, (
        "the catalog Deployment renders no LANCE_CONTROL_OUTBOX_URI, so `DaprControlEmitter.emit` degrades "
        "to a plain publish: a NATS blip during a publication loses `table_published` outright, and with it "
        "the silver->gold hop that event is the only trigger for"
    )
    lineage_prefix = env.get("LANCE_LINEAGE_OUTBOX_URI", "")
    assert staged.rstrip("/") != lineage_prefix.rstrip("/"), (
        f"the control lane stages to the LINEAGE prefix ({staged!r}) — lineage's reconcile cron parses every "
        "object there as an OpenLineage RunEvent and DELETES what it cannot parse, so each staged control "
        "event is destroyed as poison by the very drain that was supposed to save it"
    )


def test_the_relay_binding_name_is_the_route_the_catalog_serves() -> None:
    """Component name, injected env and served path are ONE string.

    The served path is probed in a SUBPROCESS on purpose: the route is mounted from `get_settings()` at
    import, which is `lru_cache`d, so setting the env in this process and reloading would re-use the
    cached settings and degrade the assertion to "the chart default equals the code default" — passing
    on a real drift and failing on a legitimate `bindingName` edit. A fresh process is what a pod is.
    """
    docs = _render()
    binding = _relay_component(docs)["metadata"]["name"]
    env = _catalog_env(docs)

    assert env.get("LANCE_CONTROL_RELAY_BINDING_NAME") == binding, (
        f"the cron Component is named {binding!r} but the catalog is told {env.get('LANCE_CONTROL_RELAY_BINDING_NAME')!r} — "
        "every tick is delivered to a route the service does not serve, and the outbox never drains"
    )

    probe = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import json,catalog.main as m; print(json.dumps(sorted(m.app.openapi()['paths'])))"],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "LANCE_CONTROL_RELAY_BINDING_NAME": binding,
            "LANCE_S3_ACCESS_KEY_ID": env.get("LANCE_S3_ACCESS_KEY_ID", "k"),
            "LANCE_S3_SECRET_ACCESS_KEY": "probe-secret",
        },
        cwd=REPO,
    )
    served = json.loads(probe.stdout)
    assert f"/{binding}" in served, f"the catalog serves no route at /{binding} — the cron Component ticks into a 404 on every schedule"
    assert not any(path.endswith(f"/{binding}") and path != f"/{binding}" for path in served), (
        "the relay route is mounted under a prefix — a Dapr input binding is delivered at the POD ROOT, never under RASK_API_PREFIX"
    )


def test_staging_and_the_relay_are_ONE_switch() -> None:
    """Half of this mechanism is worse than neither half: staging with no relay accumulates a durable
    copy nothing reads, and a relay with no staging drains an empty prefix forever."""
    off = _render("catalog.controlOutbox.enabled=false")
    env = _catalog_env(off)

    assert "LANCE_CONTROL_OUTBOX_URI" not in env, "catalog.controlOutbox.enabled=false must also stop the staging (its relay is off with it)"
    assert "LANCE_CONTROL_RELAY_BINDING_NAME" not in env, "the relay route is mounted with no prefix to drain"
    assert not [
        doc
        for doc in off
        if doc.get("kind") == "Component" and (doc.get("spec") or {}).get("type") == "bindings.cron" and "catalog" in (doc.get("scopes") or [])
    ], "the cron Component still renders with the outbox off — it would tick against an unmounted route"
