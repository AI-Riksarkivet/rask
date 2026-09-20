"""Every topic the chart tells a workload to publish to is captured by a stream the chart creates.

[[LH-151]]. A workload configured to publish to a subject no stream captures is a HALF
configuration, and it does not announce itself: the env var is present, the component is healthy,
and the gap only shows when something is actually published.

MEASURED ON THE LIVE ESTATE 2026-09-20, which is why this exists. `rask-bronze-to-silver` carried
`MEDALLION_REFUSED_TOPIC=refused.bronze-to-silver` while `/jsz` listed five streams and no REFUSED
— the env had been set by hand and the stream had not survived with it. The failure direction was
safe (`_settle` acks SUCCESS only when the retain publish lands, so the trigger parks rather than
being acked into nothing), but safe is not the same as working: the feature was configured and
inert.

WHAT THIS GATE CAN AND CANNOT SEE, stated because the difference is the whole lesson. It renders the
CHART and compares its two halves, so it catches a topic added without a stream, or a stream removed
while topics still name it. It CANNOT see a hand-set `kubectl set env` on a running cluster, which is
what actually happened — nothing in a repo can. That asymmetry is the argument for the chart owning
both halves rather than an operator owning one of them.

SUBJECT MATCHING IS NATS', NOT STRING EQUALITY. A stream is declared over a wildcard (`medallion.>`),
so `medallion.bronze` is captured by prefix. Comparing literally would report every topic as
uncovered and the gate would be abandoned on its first run.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest


REPO = pathlib.Path(__file__).resolve().parents[2]
CHART = REPO / "chart"

#: `add_if_missing <STREAM> "<subject>"` in the NATS stream job — the chart's stream declarations.
#: Anchored past leading whitespace and REFUSING a `#`, because a commented-out declaration creates no
#: stream. Caught by mutation-checking this gate: without the anchor, commenting the REFUSED line out
#: still read as declared and the gate stayed green over exactly the hole it exists to find.
_STREAM = re.compile(r'^[^\S\n]*add_if_missing ([A-Z_]+) "([^"]+)"', re.MULTILINE)
#: Any rendered env var naming a topic. Deliberately broad: a new `*_TOPIC` is covered by existing.
_TOPIC_ENV = re.compile(r'name: ([A-Z_]*TOPIC[A-Z_]*), value: "([^"]*)"')


def _render() -> str:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not pathlib.Path(helm).exists():
        pytest.skip("helm not available")
    argv = [
        helm,
        "template",
        str(CHART),
        "--set",
        "image.localImages=true",
        "--set-string",
        "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum",
        "--set-string",
        "frontend.oidc.publicIssuer=http://localhost:8080/dex",
        "--set-string",
        "frontend.oidc.publicOrigin=http://localhost:8080",
    ]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603


def _captures(subject: str, declared: str) -> bool:
    """NATS subject matching, for the two forms this chart declares: a `>` tail or a literal."""
    return subject.startswith(declared[:-1]) if declared.endswith(".>") else subject == declared


@pytest.fixture(scope="module")
def rendered() -> str:
    return _render()


def test_every_published_topic_is_captured_by_a_stream_the_chart_creates(rendered: str) -> None:
    streams = dict(_STREAM.findall(rendered))
    topics = {(name, value) for name, value in _TOPIC_ENV.findall(rendered) if value}

    uncaptured = sorted(f"{name}={value!r}" for name, value in topics if not any(_captures(value, declared) for declared in streams.values()))

    assert not uncaptured, (
        "these workloads are told to publish to a subject NO stream captures, so the publish has "
        f"nowhere to land — add the stream in the NATS stream job or drop the topic. Streams "
        f"declared: {sorted(streams)}\n  " + "\n  ".join(uncaptured)
    )


def test_the_render_actually_produced_both_halves(rendered: str) -> None:
    """Without this, a render that produced neither streams nor topics would pass vacuously."""
    streams = dict(_STREAM.findall(rendered))
    topics = {value for _, value in _TOPIC_ENV.findall(rendered) if value}

    assert len(streams) >= 5, f"only {len(streams)} streams declared — the stream job did not render"
    assert len(topics) >= 10, f"only {len(topics)} topics rendered — the walk is broken, not clean"
