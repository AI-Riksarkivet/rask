"""Emission must be REAL in a rendered production config — not merely configured somewhere.

A lineage plane fails silently by construction: every emitter in this repo logs and swallows on the
error path, so a misrouted event produces a green deployment, a healthy pod, and an empty graph.
Nothing about the running system distinguishes "no lineage was emitted" from "nothing has happened
yet". That is why the wiring has to be asserted at render time rather than observed at runtime.

The lakehouse's producers emit through **Dapr pub/sub**: maintenance and the catalog each publish to
``<APP>_LINEAGE_TOPIC`` through their own per-subscriber pubsub component (``lance.subPubsub``), and
``services/lineage`` subscribes on ``LINEAGE_DAPR_TOPIC``. Component names differ per app on purpose (each
carries its own queueGroupName); the TOPIC must match. ``test_invariants._PINNED_TOPICS`` already pins each
service's in-code DEFAULT, but it cannot see the chart — and the chart overrides those defaults on every
deployed pod, so a template that stopped deriving from ``.Values.pubsub.topic`` would satisfy that pin while
dropping every event. A producer outside the estate posts OpenLineage to lineage's HTTP door with its own
transport (lineage-kit's ``build_emitter``, configured by that producer), which this chart renders nothing for.

Asserted against ``helm template`` output rather than values.yaml, because the rendered manifest is what
actually runs.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.chart_render import PREREQ_ARGS


REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "chart"

#: The chart renders env in TWO styles and both must be read. The lance-ns templates use the inline
#: `- { name: FOO, value: "bar" }`; the rask fleet templates (gateway, controlplane, …) use the block
#: form on two lines. A regex for only the inline form silently exempts half the estate from every
#: assertion here — which is precisely the "green gate that checks nothing" failure this file is about.
_ENV = re.compile(r"\{\s*name:\s*([A-Z0-9_]+),\s*value:\s*\"([^\"]*)\"\s*\}|-\s*name:\s*([A-Z0-9_]+)\s*\n\s*value:\s*\"([^\"]*)\"")


def _env_pairs(rendered: str) -> list[tuple[str, str]]:
    """(name, value) for every env var in the render, in document order, both styles."""
    return [(m[0] or m[2], m[1] or m[3]) for m in _ENV.findall(rendered)]


#: The subscriber. If this is wrong or absent, nothing reaches the graph no matter who publishes.
_SUBSCRIBER_TOPIC = "LINEAGE_DAPR_TOPIC"

#: Every env var that names the topic a service PUBLISHES lineage to. Each must equal the
#: subscriber's. `LANCE_DAPR_TOPIC` is the catalog's (it emits its own table-lifecycle runs).
_PUBLISHER_TOPICS = ("MAINTENANCE_LINEAGE_TOPIC", "LANCE_DAPR_TOPIC")


def _render(*set_values: str) -> str:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", "rask", str(CHART)]
    # Since auth defaults ON (2026-08-06) every render needs identity values; the chart refuses OIDC
    # without a session secret ON PURPOSE, and that refusal has its own test in test_invariants.py.
    argv += PREREQ_ARGS
    argv += ["--set-string", "auth.oidc.issuer=http://localhost:8080/dex"]
    # Side-loaded images: see `rask.image` in _helpers.tpl — the chart refuses a bare
    # `<component>:<tag>` unless this is set, because that is docker.io and not a local image.
    argv += ["--set", "image.localImages=true"]
    for value in set_values:
        argv += ["--set", value]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return proc.stdout


def _env_values(rendered: str, name: str) -> list[str]:
    return [value for key, value in _env_pairs(rendered) if key == name]


def test_every_lineage_publisher_renders_the_subscribers_topic() -> None:
    """Publisher topics == subscriber topic, in the rendered chart."""
    rendered = _render()

    subscribed = _env_values(rendered, _SUBSCRIBER_TOPIC)
    assert subscribed, f"{_SUBSCRIBER_TOPIC} is not rendered — services/lineage subscribes to its in-code default while the chart moves the publishers"
    assert len(set(subscribed)) == 1, f"the lineage subscriber is rendered with conflicting topics: {sorted(set(subscribed))}"
    topic = subscribed[0]
    assert topic, f"{_SUBSCRIBER_TOPIC} rendered EMPTY — the subscription binds to nothing and every event is dropped"

    published: dict[str, list[str]] = {name: _env_values(rendered, name) for name in _PUBLISHER_TOPICS}
    assert any(published.values()), f"no lineage publisher renders a topic at all: {published}"

    mismatched = {name: sorted(set(values)) for name, values in published.items() if values and set(values) != {topic}}
    assert not mismatched, (
        f"these services publish lineage to a topic nobody subscribes to (subscriber is on {topic!r}): {mismatched} — "
        "every event they emit is silently discarded by the broker, and the emit path logs nothing above DEBUG"
    )


def test_the_lineage_subscriber_is_actually_subscribing() -> None:
    """A rendered-but-disabled subscriber drops everything just as thoroughly as a wrong topic.

    ``lineage.api.dapr`` always constructs ``DaprApp`` (it serves ``/dapr/subscribe`` regardless), so
    the pod is healthy and the endpoint answers whether or not the subscription itself was registered.
    """
    rendered = _render()
    enabled = _env_values(rendered, "LINEAGE_DAPR_ENABLED")
    assert enabled, "LINEAGE_DAPR_ENABLED is not rendered — whether the lineage service subscribes is left to a default the chart does not state"
    assert set(enabled) == {"true"}, (
        f"the lineage subscriber is rendered disabled ({enabled}) — the graph would stay empty while every producer reports success"
    )
