"""The inbox's reads of lineage's feed are retried by its own sidecar, a few seconds apart, and never on an answer that cannot change.

The reconcile tick reads lineage's `GET /events` through service invocation (`/v1.0/invoke/lineage/method`), and Dapr
applies an invocation policy on the CALLING sidecar, so the one retry layer of those reads is this release's Resiliency,
scoped to the inbox and targeting app `lineage` (the reconciler has no retry of its own). `constant` is the one policy
whose `duration` dapr/kit reads. A 4xx is never retried: a refusal's answer will not change on a second ask.

The API server prunes a field a CRD does not declare, so a misspelled key applies silently: a `matching` typo becomes a
retry on every answer, a 403 included. The fields below are the ones `resiliencies.dapr.io` declares on the Dapr the
`dapr` fixture pins (read off the live CRD, Dapr 1.19.0-rc.4, 2026-11-06).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final


_RETRY_FIELDS: Final = frozenset({"policy", "duration", "matching", "maxInterval", "maxRetries"})
_MATCHING_FIELDS: Final = frozenset({"httpStatusCodes", "gRPCStatusCodes"})
_APP_TARGET_FIELDS: Final = frozenset({"timeout", "retry", "circuitBreaker", "circuitBreakerCacheSize"})


def test_the_inbox_retries_lineage_three_times_two_seconds_apart_on_a_transient_answer_alone(
    render_console: Callable[..., tuple[dict, ...]],
) -> None:
    [resiliency] = [doc for doc in render_console() if doc.get("kind") == "Resiliency" and "apps" in doc["spec"]["targets"]]

    apps = resiliency["spec"]["targets"]["apps"]
    policy = resiliency["spec"]["policies"]["retries"][apps["lineage"]["retry"]]

    assert (set(policy) - _RETRY_FIELDS, set(policy["matching"]) - _MATCHING_FIELDS, set(apps["lineage"]) - _APP_TARGET_FIELDS) == (set(), set(), set())
    assert (policy["policy"], policy["duration"], policy["maxRetries"], policy["matching"]) == ("constant", "2s", 3, {"httpStatusCodes": "408,429,500-599"})
    assert (list(apps), resiliency["scopes"]) == (["lineage"], ["notifications"])
