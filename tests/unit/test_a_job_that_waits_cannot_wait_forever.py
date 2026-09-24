"""A chart Job that waits on a dependency must carry a wall-clock ceiling.

THE THREE BOUNDS A JOB CAN HAVE ARE NOT INTERCHANGEABLE, which is the whole finding.
`backoffLimit` counts POD failures. `ttlSecondsAfterFinished` reaps a job that has COMPLETED. Neither
touches a pod that is healthy and stuck — and that is exactly what these Jobs become, because each
waits on a dependency with a shell loop:

    until mc alias set rfs <endpoint> <key> "$MINIO_SECRET_KEY"; do echo wait-minio; sleep 3; done

If the dependency never answers, the loop never exits, the container never exits, the pod never
fails, `backoffLimit` never counts, and the TTL never fires because nothing completed.

MEASURED on the live estate 2026-09-24: `rask-minio-scoped-users-r229` Running 27h, its pod still
printing `wait-minio` and `mkdir /.mc: read-only file system`, having provisioned nothing. Its TTL was
set and could never apply. It was one of SIX rendered Jobs that wait and had no deadline;
`activeDeadlineSeconds` is the only one of the three that bounds this shape.

DETECTED ON THE COMMAND, not on a list of job names. A Job earns the requirement by containing a wait
— `until … do`, `while : ; do`, `for i in $(seq …)` — so a new bootstrap Job that polls something is
covered the day it is written, and one that does not poll is not asked for a ceiling it does not need.
"""

from __future__ import annotations

import re

from tests.unit.chart_render import OIDC_ARGS, render


#: Shell shapes that block on something outside the pod.
_WAITS = re.compile(r"\buntil\b[^\n]*\bdo\b|while\s*:\s*;\s*do|for i in \$\(seq")

_OVERLAY = ("--set", "image.localImages=true", "--set", "auth.enabled=true")


def _jobs() -> list[tuple[str, bool, int | None]]:
    """`(name, waits, activeDeadlineSeconds)` for every Job and CronJob the chart renders."""
    out = []
    for doc in render(*_OVERLAY, *OIDC_ARGS):
        kind = doc.get("kind")
        if kind not in ("Job", "CronJob"):
            continue
        spec = doc["spec"] if kind == "Job" else (doc["spec"].get("jobTemplate") or {}).get("spec", {})
        pod = (spec.get("template") or {}).get("spec") or {}
        command = " ".join(
            " ".join(str(part) for part in (container.get("command") or []) + (container.get("args") or []))
            for container in (pod.get("containers") or []) + (pod.get("initContainers") or [])
        )
        out.append((doc["metadata"]["name"], bool(_WAITS.search(command)), spec.get("activeDeadlineSeconds")))
    return out


def test_the_render_produces_jobs_that_wait() -> None:
    """A control: with nothing detected as waiting, the gate below passes by vacuum."""
    waiting = [name for name, waits, _ in _jobs() if waits]
    assert waiting, "no rendered Job matches the wait shapes — the detector or the render moved"


def test_every_waiting_job_has_a_deadline() -> None:
    offenders = [name for name, waits, deadline in _jobs() if waits and deadline is None]
    assert not offenders, (
        "these Jobs block on a dependency with no `activeDeadlineSeconds`, so a dependency that never "
        "answers leaves them Running forever — the container does not exit, so the pod does not fail, "
        "so `backoffLimit` never counts and `ttlSecondsAfterFinished` never applies:\n  " + "\n  ".join(sorted(offenders))
    )
