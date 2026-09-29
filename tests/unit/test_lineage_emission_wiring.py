"""Emission must be REAL in a rendered production config — not merely configured somewhere.

A lineage plane fails silently by construction: every emitter in this repo logs and swallows on the
error path (``ClientEmitter.emit`` catches ``Exception``; ``NoopEmitter`` logs at DEBUG and drops), so
a misrouted event produces a green deployment, a healthy pod, and an empty graph. Nothing about the
running system distinguishes "no lineage was emitted" from "nothing has happened yet". That is why
the wiring has to be asserted at render time rather than observed at runtime.

**TWO independent lineage paths exist**, and both are asserted here because either one failing is
invisible from outside:

1. **Dapr pub/sub** — what the medallion producer/stage runners, compaction and the catalog use TODAY. Each
   publishes to ``<APP>_LINEAGE_TOPIC`` through its own per-subscriber pubsub component
   (``lance.subPubsub``); ``services/lineage`` subscribes on ``LINEAGE_DAPR_TOPIC``. Component names
   differ per app on purpose (each carries its own queueGroupName); the TOPIC must match.
   ``test_invariants._PINNED_TOPICS`` already pins each service's in-code DEFAULT, but it cannot see
   the chart — and the chart overrides those defaults on every deployed pod, so a template that
   stopped deriving from ``.Values.pubsub.topic`` would satisfy that pin while dropping every event.

2. **lineage-kit's HTTP transport** — ``LineageRun.emitter`` → ``default_emitter()`` →
   ``build_emitter()``, reading ``RASK_LINEAGE_ENDPOINT``. This is the seam ``@stage`` and the actor
   helpers use, i.e. what the Ray Data pipeline (P7b) will emit through. It has no callers *yet*,
   which is exactly why the wiring must land first: with no endpoint ``build_emitter`` logs one
   ``log.warning("lineage_http_without_endpoint …")`` at startup and returns ``NoopEmitter``, which
   drops every subsequent event at DEBUG. The first real actor run must not be the moment anyone
   discovers the wire was never connected — the same reason the ``parent`` facet is parsed before
   anything emits one.

Both are asserted against ``helm template`` output rather than values.yaml, because the rendered
manifest is what actually runs.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


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
_PUBLISHER_TOPICS = ("MEDALLION_LINEAGE_TOPIC", "MAINTENANCE_LINEAGE_TOPIC", "LANCE_DAPR_TOPIC")


def _render(*set_values: str) -> str:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", "rask", str(CHART)]
    # Since auth defaults ON (2026-08-06) every render needs identity values; the chart refuses OIDC
    # without a session secret ON PURPOSE, and that refusal has its own test in test_invariants.py.
    argv += ["--set-string", "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum"]
    argv += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    argv += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
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
    """Publisher topics == subscriber topic, in the rendered chart.

    Compaction is rendered explicitly because it is off by default — a publisher that only appears
    under a toggle is exactly the one that drifts unnoticed.
    """
    rendered = _render("compaction.enabled=true")

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


def test_rendered_config_does_not_degrade_to_a_noop_emitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The condition item-4 names: ``build_emitter()`` under a RENDERED production config is not a no-op.

    Takes the endpoint the chart actually renders and feeds it to the real factory. Asserting the env
    var "is present" would not be enough — an endpoint that is rendered empty, or a ``transport``
    default that changed to ``noop``, both leave the var present and the emitter dead.
    """
    from lineage_kit.emitter import NoopEmitter, build_emitter

    rendered = _render("ray.enabled=true", "singleTenant.enabled=true", "compaction.enabled=true")
    endpoints = set(_env_values(rendered, "RASK_LINEAGE_ENDPOINT"))
    assert endpoints, "the chart renders RASK_LINEAGE_ENDPOINT nowhere — lineage-kit's HTTP transport degrades to NoopEmitter on every pod"
    assert len(endpoints) == 1, f"emitting services disagree on the lineage endpoint: {sorted(endpoints)}"

    # Hermetic: the official client's own alias would otherwise let an ambient value mask a missing render.
    monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
    monkeypatch.delenv("RASK_LINEAGE_TRANSPORT", raising=False)
    for path in set(_env_values(rendered, "RASK_LINEAGE_ENDPOINT_PATH")) or {"api/v1/lineage"}:
        monkeypatch.setenv("RASK_LINEAGE_ENDPOINT_PATH", path)
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", endpoints.pop())

    emitter = build_emitter()
    assert not isinstance(emitter, NoopEmitter), (
        "build_emitter() degraded to NoopEmitter under the rendered production config — every event "
        "lineage-kit's @stage/actor seam emits would be dropped, announced by one startup log.warning"
    )


def test_every_lineage_producer_renders_the_endpoint() -> None:
    """Every LINEAGE PRODUCER carries the transport — a producer without it is silently unlineaged.

    "Emitting service" is defined as a container that renders a lineage TOPIC (``_PUBLISHER_TOPICS``),
    not merely one that renders ``OTEL_SERVICE_NAME``. The distinction is load-bearing in both
    directions: keying on the OTel name would demand the endpoint from ``gateway`` and
    ``controlplane``, which emit no lineage at all (a stateless proxy and a read-only CR reader), while
    an earlier draft of this test keyed on it AND used an inline-only env regex — so it silently
    exempted exactly those two and looked green either way.

    Self-maintaining: a new producer is caught the moment it renders a lineage topic, which it must do
    to publish at all.
    """
    rendered = _render("ray.enabled=true", "singleTenant.enabled=true", "compaction.enabled=true")

    # Walk env in document order so each var is attributed to the container it sits in. OTEL_SERVICE_NAME
    # is the per-container marker; the topic/endpoint that follow belong to that service.
    producers: dict[str, dict[str, bool]] = {}
    current: str | None = None
    for key, value in _env_pairs(rendered):
        if key == "OTEL_SERVICE_NAME":
            current = value
            producers.setdefault(current, {"emits": False, "transport": False})
        elif current is None:
            continue
        elif key in _PUBLISHER_TOPICS:
            producers[current]["emits"] = True
        elif key == "RASK_LINEAGE_ENDPOINT":
            producers[current]["transport"] = True

    assert producers, "no containers parsed out of the render — the env regex no longer matches the chart"
    emitting = {name for name, s in producers.items() if s["emits"]}
    assert emitting, f"no lineage producers found at all; parsed services: {sorted(producers)}"

    missing = sorted(name for name in emitting if not producers[name]["transport"])
    assert not missing, (
        f"these lineage producers render no RASK_LINEAGE_ENDPOINT: {missing} — anything they emit "
        "through lineage-kit's @stage/actor seam is dropped by a NoopEmitter while their neighbours "
        "(and their Dapr path) keep reporting success"
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


# ── the INGEST plane's lineage identity — the second lane to lose all provenance ─────
