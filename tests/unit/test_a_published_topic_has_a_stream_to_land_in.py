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

#: `add_if_missing` OR `add_workqueue_if_missing <STREAM> "<subject>"` — the chart declares streams
#: through BOTH helpers, and matching only the first made this gate blind to every work-queue stream it
#: creates. Measured 2026-09-22: with `maintenance.workTopic` set, the gate reported
#: `maintenance.work.v1` as landing nowhere while `add_workqueue_if_missing MAINTENANCE_WORK
#: "maintenance.work.>"` sat in the job three lines from an `assert_retention` for it — and INGEST was
#: invisible the same way. A gate that cannot see half the declarations reports a hole where there is
#: none and, worse, would miss a real one in the half it cannot read.
#:
#: Anchored past leading whitespace and REFUSING a `#`, because a commented-out declaration creates no
#: stream. Caught by mutation-checking this gate: without the anchor, commenting the REFUSED line out
#: still read as declared and the gate stayed green over exactly the hole it exists to find.
_STREAM = re.compile(r'^[^\S\n]*add(?:_workqueue)?_if_missing ([A-Z_]+) "([^"]+)"', re.MULTILINE)
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


#: The `nats` CLI prompts for any option a command does not supply, and a Job has no terminal.
_NON_INTERACTIVE = "--defaults"


def _creation_bodies(rendered: str) -> dict[str, str]:
    """``{helper name: its body}`` for each `add_*_if_missing` shell function in the rendered Job."""
    found = {}
    for match in re.finditer(r"^\s*(add(?:_workqueue)?_if_missing)\(\)\s*\{(.*?)^\s*\}", rendered, re.MULTILINE | re.DOTALL):
        found[match.group(1)] = match.group(2)
    return found


def test_the_walk_finds_both_creation_helpers(rendered: str) -> None:
    """Two helpers exist and both create streams; a walk that found one would half-cover the rule."""
    assert set(_creation_bodies(rendered)) == {"add_if_missing", "add_workqueue_if_missing"}


def test_a_stream_is_created_WITHOUT_ASKING_because_a_Job_has_no_terminal(rendered: str) -> None:
    """MEASURED LIVE 2026-09-22, and it is why this exists.

    `MAINTENANCE_INDEX` was declared in the chart, the Job ran, printed "not present yet — creation
    below will set retention=workqueue", reported **Complete 1/1** — and the stream did not exist.
    `nats stream add` had answered `invalid input: cannot ask for confirmation without a terminal`
    (nats CLI 0.4.0, `--defaults` = "Accept default values for all prompts").

    Every stream in the cluster predates this, so the whole creation path was inert and nothing said
    so: the chart-level gate above checks that a topic is DECLARED, which it was. A declaration the
    runtime cannot act on is the same hole one layer down.
    """
    for helper, body in _creation_bodies(rendered).items():
        assert _NON_INTERACTIVE in body, (
            f"`{helper}` runs `nats stream add` without `{_NON_INTERACTIVE}`, so it prompts and dies in a "
            "Job with no terminal — the stream is never created and the Job still succeeds"
        )


def test_a_FAILED_creation_fails_the_job(rendered: str) -> None:
    """The second half, and the one that made the first invisible for a whole release.

    The script has no `set -e`, so a creation that dies leaves the Job green. A topic then has a
    publisher, a component, a declaration and no stream — the exact state [[LH-151]] exists to make
    impossible, reached from the side the chart cannot see.
    """
    for helper, body in _creation_bodies(rendered).items():
        assert "exit 1" in body, f"`{helper}` does not fail the Job when `nats stream add` fails, so a missing stream reports success"
