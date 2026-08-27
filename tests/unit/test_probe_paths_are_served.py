"""Every probe the chart configures must be a path the app actually serves — for every app.

open_fastapi-audit — "The probe-path-is-actually-served gate covers exactly one of the fifteen apps,
and controlplane bypasses the mechanism the gate reads".

THE FINDING CORRECTS ITS OWN HEADLINE and this file follows the correction. "One of fifteen"
overstates it: the drift class needs a probe path DERIVED at runtime while the chart writes a
LITERAL, and only the `make_service_app` fleet layout does that. The eight lance apps root-mount
`/livez` and `/readyz` as literals and `lance.appProbes` writes those same literals, so no mismatch is
possible there. The honest claim is one of five — plus controlplane, which is rendered by its own
template and is therefore invisible to any test keyed on `services.*`, which is exactly why it kept
missing fixes.

But the gate is written for ALL of them anyway, because scoping it to the five that can drift today
would encode today's layout into the test. A future `healthPath` edit, a prefix change, or a probe
moved under `RASK_API_PREFIX` is caught wherever it happens.

Two things make this a real check rather than a restatement of the chart:

**The uvicorn target is read from the render, not from a list.** `args[0]` is `catalog.main:app`,
`ingest:create_app`, `gateway:app` — the chart already names the import path of every app it runs, so
a new Deployment is covered the moment it renders and an app that gets renamed cannot go quietly
unchecked.

**Each app is imported under the CHART'S OWN env**, assembled from that container's `envFrom`
ConfigMaps and `env` entries. That is not scaffolding to make the import succeed — it is the property
under test. `catalog` will not import at all without `LANCE_S3_ACCESS_KEY_ID`, which the chart
supplies; asking whether an app serves its probe paths is only meaningful under the configuration it
is actually given.

Run in a subprocess per app, deliberately: these apps build settings singletons and instrument
themselves at import, and importing thirteen of them into the pytest process would leave that state
behind for several thousand unrelated tests.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _first_party_deployments, _rendered_docs  # noqa: E402


#: `module.path:attribute`, the form uvicorn takes and the chart writes.
_TARGET = re.compile(r"^[\w.]+:\w+$")

#: Ambient config that must not leak into the child — the point is the chart's env, not this shell's.
_SCRUB = ("RASK_", "LANCE_", "LINEAGE_", "MEDALLION_", "OTEL_", "DAPR_")

_CHILD = """
import importlib, json, sys

target = sys.argv[1]
module, _, attr = target.partition(":")
obj = getattr(importlib.import_module(module), attr)
app = obj() if not hasattr(obj, "openapi") else obj
json.dump(sorted(app.openapi()["paths"]), sys.stdout)
"""


def _env_for(docs: list[dict], container: dict) -> dict[str, str]:
    """The environment the chart hands this container, ConfigMap references resolved."""
    env: dict[str, str] = {}
    for ref in container.get("envFrom") or []:
        name = (ref.get("configMapRef") or {}).get("name")
        source = next((d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == name), None)
        if source:
            env.update({k: str(v) for k, v in (source.get("data") or {}).items()})
    for entry in container.get("env") or []:
        # A `valueFrom` is a secret or a downward-API field — not resolvable from a render, and never
        # what decides where a route mounts. A placeholder keeps the import honest without inventing
        # a credential.
        env[entry["name"]] = str(entry["value"]) if "value" in entry else "rendered-placeholder"
    return env


def _uvicorn_apps() -> list[tuple[str, str, dict]]:
    """(deployment/container, import target, chart env) for every HTTP-probed first-party app."""
    docs = _rendered_docs()
    found = []
    for doc in _first_party_deployments(docs):
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            probes = [container.get(k) for k in ("startupProbe", "livenessProbe", "readinessProbe")]
            paths = sorted({(p or {}).get("httpGet", {}).get("path") for p in probes} - {None})
            if not paths:
                continue
            argv = [*(container.get("command") or []), *(container.get("args") or [])]
            target = next((a for a in argv if _TARGET.match(a)), None)
            if target is None:
                continue  # not a python app we can import (the web zones are TCP-probed anyway)
            found.append((f"{doc['metadata']['name']}/{container['name']}", target, {"paths": paths, "env": _env_for(docs, container)}))
    return found


_APPS = _uvicorn_apps()

assert _APPS, "no importable HTTP-probed app was found in the render — this gate would pass vacuously"


def test_the_gate_covers_more_than_one_app() -> None:
    """The finding IS that this was scoped to a single service; a regression to that must fail here."""
    assert len(_APPS) >= 5, f"only {len(_APPS)} apps are covered: {[name for name, _, _ in _APPS]}"


def _served_paths(name: str, target: str, chart_env: dict[str, str]) -> set[str]:
    """Import the app in a child process under the chart's env and return what it mounts."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB)}
    env.update(chart_env)
    # The chart points these at a collector that is not running here. Route mounting does not depend
    # on them, and a live exporter would have the child retrying an export on the way out.
    env.update({"OTEL_SDK_DISABLED": "true", "OTEL_TRACES_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none"})

    result = subprocess.run([sys.executable, "-c", _CHILD, target], capture_output=True, text=True, env=env, timeout=180, check=False)
    assert result.returncode == 0, f"{name}: `{target}` does not import under the env the chart gives it:\n{result.stderr[-1500:]}"
    return set(json.loads(result.stdout))


@pytest.mark.parametrize(("name", "target", "spec"), _APPS, ids=[a[0] for a in _APPS])
def test_every_probe_path_the_chart_configures_is_actually_served(name: str, target: str, spec: dict) -> None:
    """A chart probing a path the app does not mount is a CrashLoopBackOff whose cause is in neither
    file: the kubelet reports a failing probe, and the app's logs show a healthy process."""
    served = _served_paths(name, target, spec["env"])
    missing = sorted(set(spec["paths"]) - served)
    assert not missing, f"{name}: the kubelet probes {missing}, which `{target}` does not serve (it serves {sorted(served)[:8]}…)"


def test_the_gate_can_actually_FAIL() -> None:
    """A negative control, because every assertion above is green at HEAD and a coverage gate that
    cannot distinguish a served path from an unserved one is decoration.

    The finding is missing COVERAGE — it verified there is no live mismatch — so passing is the
    expected result and proves nothing on its own. This asks the same machinery about a path no app
    mounts and requires it to say no.
    """
    name, target, spec = _APPS[0]
    served = _served_paths(name, target, spec["env"])
    assert "/a-path-no-app-mounts" not in served, "the child reports every path as served, so the gate above cannot fail"
    assert served, f"{target} reported no paths at all — the gate above would pass vacuously for it"
