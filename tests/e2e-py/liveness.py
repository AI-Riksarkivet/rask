"""Waiting for a service before deciding it is not there ([[XC-039]]).

A SKIP THAT READS AS GREEN IS THE DEFECT. Seventeen `/livez` probes across this directory carry a bare
`timeout=5` and skip the module on ANY exception, so a producer that is up and serving the cascade —
merely loaded, mid-rollout, or waiting on a cold Lance open — turns a suite that would have passed
into one that reports "not reachable". Nothing downstream can tell that apart from a genuine absence,
which is how a live suite quietly stops covering anything.

THE BUDGET IS THE FIX, NOT A BIGGER TIMEOUT. One long timeout still fails a service that is restarting
during the probe, and it makes every genuinely-absent target cost the whole timeout. A bounded retry
inside a total budget answers both: a healthy-but-slow service is waited for, and an absent one costs
one attempt's timeout per interval until the budget runs out.

THE SKIP MESSAGE SAYS HOW LONG IT WAITED, because the reason this went unnoticed is that "not
reachable at <url>" is indistinguishable from "not configured" once it reaches a CI log.
"""

from __future__ import annotations

import time
from collections.abc import Callable


#: Per-attempt HTTP timeout. Generous rather than tight: an attempt that gives up in 5 s is what this
#: module exists to replace, and a slow answer is still an answer.
ATTEMPT_TIMEOUT_SECONDS = 15.0

#: Total wall-clock to keep trying before calling a target absent. Chosen against what the estate
#: actually does: a rolling Deployment's new pod passes readiness within ~30 s on this cluster, so a
#: budget under that would still skip across an ordinary rollout.
BUDGET_SECONDS = 60.0

#: Gap between attempts. Small enough that a service coming up is caught promptly, large enough that a
#: genuinely absent target is not hammered for the whole budget.
INTERVAL_SECONDS = 3.0


def wait_until_live(
    url: str,
    *,
    probe: Callable[[str, float], None],
    budget_seconds: float = BUDGET_SECONDS,
    interval_seconds: float = INTERVAL_SECONDS,
    attempt_timeout_seconds: float = ATTEMPT_TIMEOUT_SECONDS,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, float, str]:
    """Poll ``url`` until it answers or the budget is spent.

    Returns ``(live, waited_seconds, last_error)``. It does NOT skip: the decision of what an absent
    service means belongs to the caller, and a helper that called `pytest.skip` itself could not be
    tested without pytest's own machinery.

    ``probe``, ``now`` and ``sleep`` are injected so the policy — how long, how often, what counts as
    up — is testable without a live service and without the test sleeping for a minute. The real
    caller passes an HTTP GET; a test passes a script.
    """
    started = now()
    last_error = ""
    while True:
        try:
            probe(url, attempt_timeout_seconds)
        except Exception as exc:  # noqa: BLE001 — any failure is "not up yet"; the reason is reported
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            return True, now() - started, ""
        waited = now() - started
        # CHECKED AFTER THE ATTEMPT, so a budget shorter than one attempt still makes one — otherwise a
        # zero budget would report "not reachable" having never asked.
        if waited >= budget_seconds:
            return False, waited, last_error
        sleep(interval_seconds)
