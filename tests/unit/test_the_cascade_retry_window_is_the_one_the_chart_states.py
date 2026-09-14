"""The sidecar retry window the chart CONFIGURES is the one it DESCRIBES.

[[LH-106]] gap #2. A retry schedule is prose until something measures it, and the two can disagree by
two orders of magnitude without anything failing — the sidecar reads its policy, the app answers
RETRY, deliveries get parked, and every part looks healthy while the window is 4 seconds instead of
the 7.5 minutes the chart describes.

WHY THE PAIRING IS THE WHOLE THING. dapr/kit's `NewBackOff` reads `Duration` **only** under
`PolicyConstant` (`b = backoff.NewConstantBackOff(c.Duration)`); under `PolicyExponential` it is
ignored entirely and the schedule comes from `InitialInterval`/`Multiplier`/`RandomizationFactor`/
`MaxInterval`. So `policy: exponential` beside `duration: 30s` is a number with no effect, and the
schedule silently falls to cenkalti/backoff's defaults — 500ms initial, x1.5, 0.5 randomization —
giving 0.5 + 0.75 + 1.125 + 1.6875 = 4.06s of mean interval.

MEASURED, not derived: driving a poison delivery to `bronze-to-silver` on 2026-09-14 under that
pairing parked it after **3.553s** over five attempts, with gaps of 0.665 / 0.550 / 0.868 / 1.470s —
each inside its jittered band around the predicted steps. That is the arithmetic this module encodes,
confirmed against the cluster rather than assumed from the source.

THE CRD CANNOT EXPRESS AN EXPONENTIAL SCHEDULE WITH A LONG FIRST STEP, which is why `constant` is a
forced choice rather than a preference. `resiliencies.dapr.io` declares exactly five keys under a
retry — `policy`, `duration`, `matching`, `maxInterval`, `maxRetries` (read off the live CRD,
2026-09-14). `initialInterval` and `multiplier` are not among them, so under `exponential` the first
step is pinned at 500ms no matter what anyone writes. `constant` + `duration` is the only shape that
reaches minutes, and the only DETERMINISTIC one: `randomizationFactor` is equally unsettable, so an
exponential window varies +/-50% against a margin the template calls load-bearing.

WHAT THIS GATES IS THE ARITHMETIC, NOT A LITERAL. Asserting `policy: constant` alone would pass for a
2-second window; asserting `duration: 30s` alone would pass for the exponential pairing that measures
4s. So the window is COMPUTED from the rendered policy under the upstream semantics and checked
against both edges it must sit between: long enough to outlive a dependency restart, and short enough
— counting the handler's own time, not just the backoff — that the broker's `ackWait`, read from the
rendered component rather than a literal, never redelivers underneath a retry still in flight.

The two INVOCATION policies in the same file use `constant` + `duration`, so the correct idiom is
already present beside the one this gate protects.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from tests.unit.test_invariants import _helm_template


#: dapr/kit takes these from cenkalti/backoff/v4 and the Resiliency CRD exposes none of them, so an
#: `exponential` policy gets these values whatever the chart writes.
BACKOFF_INITIAL_SECONDS = 0.5
BACKOFF_MULTIPLIER = 1.5

#: A dependency restart is the event the window exists to survive: measured on this estate, a catalog
#: or AGE pod is back inside ~60s and MinIO inside ~90s. 300s is that with room, and it is well under
#: the 720s ack window the components carry.
MINIMUM_USEFUL_WINDOW_SECONDS = 300.0

#: How long one failing attempt may occupy the app before it answers RETRY. Generous on purpose — it
#: is the scale of an S3 or catalog connect timeout, not of the 10ms a missing-dataset probe took when
#: this was driven live. It exists so the ack-window check counts attempts, not only backoff.
HANDLER_ATTEMPT_BUDGET_SECONDS = 30.0

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m)$")


def _seconds(literal: str) -> float:
    """Parse a Dapr duration literal (`30s`, `720s`, `500ms`) into seconds."""
    match = _DURATION.match(literal.strip())
    if match is None:
        raise ValueError(f"unparseable Dapr duration: {literal!r}")
    value, unit = float(match.group(1)), match.group(2)
    return value * {"ms": 0.001, "s": 1.0, "m": 60.0}[unit]


def _window_seconds(policy: dict[str, Any]) -> float:
    """Total backoff a delivery gets before it is dead-lettered, under dapr/kit's own semantics.

    `constant` sums `maxRetries` steps of `duration`. `exponential` ignores `duration` outright and
    sums `initialInterval * multiplier**n` capped at `maxInterval` — which is the whole defect, so the
    branch is written the way the upstream reads it rather than the way the chart describes it.
    """
    retries = int(policy["maxRetries"])
    if policy.get("policy") == "constant":
        return retries * _seconds(policy["duration"])

    cap = _seconds(policy["maxInterval"]) if "maxInterval" in policy else 60.0
    return sum(min(BACKOFF_INITIAL_SECONDS * BACKOFF_MULTIPLIER**step, cap) for step in range(retries))


@pytest.fixture(scope="module")
def rendered() -> list[dict[str, Any]]:
    """Every chart document, with the pub/sub resiliency plane switched on."""
    raw = _helm_template("dapr.enabled=true", "dapr.resiliency.enabled=true", "medallion.enabled=true")
    return [doc for doc in yaml.safe_load_all(raw) if doc]


@pytest.fixture(scope="module")
def retry_policies(rendered: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Name -> policy for every retry the chart renders, across both Resiliency CRs."""
    policies: dict[str, dict[str, Any]] = {}
    for doc in rendered:
        if doc.get("kind") != "Resiliency":
            continue
        policies.update(doc["spec"]["policies"].get("retries", {}))
    assert policies, "no retry policies rendered at all — this suite would pass by testing nothing"
    return policies


def test_a_policy_that_sets_duration_uses_the_policy_that_READS_it(retry_policies: dict[str, dict[str, Any]]) -> None:
    """`duration` is meaningful under `constant` and ignored under `exponential` (dapr/kit `NewBackOff`).

    Stated over EVERY rendered retry rather than the one that broke: the defect is a mismatched pair,
    not a bad value, and the pair can be mismatched in any policy someone adds next. A retry that sets
    `duration` while running `exponential` is a number with no effect — the reader believes the window
    is the duration, and it is 500ms.
    """
    mismatched = {name: policy for name, policy in retry_policies.items() if "duration" in policy and policy.get("policy") != "constant"}

    assert not mismatched, f"these policies set `duration` under a policy that ignores it: {mismatched}"


def test_the_cascade_retry_window_survives_a_dependency_restart(retry_policies: dict[str, dict[str, Any]]) -> None:
    """The pub/sub window must outlive the restart of something the handler depends on.

    This is the row's own measurement in gate form. A delivery that gives up in 3.5 seconds parks a
    cascade trigger for any blip longer than a blink — and parking is terminal: `/dlq-event` ERROR-logs
    and ACKs, deliberately never re-queues, so the stage only runs again if a human replays it.
    """
    window = _window_seconds(retry_policies["pubsubDeliveryRetry"])

    assert window >= MINIMUM_USEFUL_WINDOW_SECONDS, (
        f"the cascade retry window is {window:.2f}s, under the {MINIMUM_USEFUL_WINDOW_SECONDS:.0f}s a dependency restart needs; "
        f"rendered policy: {retry_policies['pubsubDeliveryRetry']}"
    )


def test_the_retry_window_stays_under_the_ack_window_of_every_component_it_targets(
    rendered: list[dict[str, Any]],
    retry_policies: dict[str, dict[str, Any]],
) -> None:
    """The other edge, and the reason the window cannot simply be made enormous.

    While the sidecar is retrying, the broker is holding the original un-acked. If the schedule
    outlasts `ackWait`, JetStream redelivers underneath it and the same trigger runs twice
    concurrently. `ackWait` is read off the rendered components the Resiliency CR names, so raising one
    without the other cannot pass here.

    THE HANDLER'S OWN TIME IS PART OF THE SCHEDULE, and counting only the backoff is how a window that
    fits on paper overruns in the cluster: every attempt also occupies the app for as long as it takes
    to fail. So the budget checked here is backoff PLUS one attempt-budget per attempt — which is also
    what makes the SHAPE of the window matter and not just its length. The same 480s spent as 16 x 30s
    instead of 4 x 120s fails this, correctly.
    """
    targeted = {name for doc in rendered if doc.get("kind") == "Resiliency" for name in doc["spec"].get("targets", {}).get("components", {})}
    ack_waits = {
        doc["metadata"]["name"]: _seconds(entry["value"])
        for doc in rendered
        if doc.get("kind") == "Component" and doc["metadata"]["name"] in targeted
        for entry in doc["spec"].get("metadata", [])
        if entry["name"] == "ackWait"
    }
    assert ack_waits, "no targeted component declares an ackWait, so this test would pass vacuously"

    policy = retry_policies["pubsubDeliveryRetry"]
    attempts = int(policy["maxRetries"]) + 1
    occupied = _window_seconds(policy) + attempts * HANDLER_ATTEMPT_BUDGET_SECONDS
    too_tight = {name: wait for name, wait in ack_waits.items() if wait <= occupied}

    assert not too_tight, (
        f"{_window_seconds(policy):.0f}s of backoff over {attempts} attempts occupies up to {occupied:.0f}s, "
        f"which outlasts these components' ackWait so the broker redelivers mid-retry: {too_tight}"
    )
