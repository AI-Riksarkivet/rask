"""Ray's own account of WHY a job ended badly.

Moved out of `ray-kit` with `ray_jobs_api`, its only consumer: the package declares `ray[default]`
for its SDK half and medallion paid for the whole engine to read three fields off a JSON body. This
model imports pydantic and nothing else.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RayJobFailure(BaseModel):
    """Ray's own account of WHY a job ended badly — the three fields `RayJob` has always declared.

    Split from `RayJob` because the two answer different questions and are read on different paths.
    `RayJob` is a LIST row (the jobs board, capped and sorted); this is a single job's post-mortem,
    read once on a terminal-bad outcome. A caller that only needs the cause should not have to
    construct, validate and carry the other eight fields to get at it.

    `extra="ignore"` for the same reason `RayJob` sets it: Ray's `JobDetails` carries `runtime_env`
    (the job's full pip list, env vars and working-dir refs) and an arbitrary user `metadata` dict, and
    retaining either here would put a job's whole environment into whatever this is rendered into.
    """

    model_config = ConfigDict(extra="ignore")
    error_type: str | None = None
    #: Ray's driver message. UNBOUNDED — this can be an entire traceback, so every consumer passes a
    #: limit to `summary()` rather than interpolating it directly.
    message: str | None = None
    #: The driver's process exit code. 137 is SIGKILL, i.e. the host-RAM OOM that kills a stage with no
    #: other symptom; 1 is an ordinary Python exception. Distinguishing them is the whole point.
    driver_exit_code: int | None = None

    def summary(self, message_limit: int) -> str:
        """A one-line cause, with the message bounded by the CALLER's limit.

        The limit is an argument rather than a constant here because the ceiling belongs to whatever
        this string is being put INTO — a lineage event published through a claim-check funnel has a
        different budget from a log line — and `ray-kit` has no business knowing which.

        Ordered type-first so truncation can only ever eat the free-text tail: the error TYPE and the
        exit CODE are what classify a failure, and a cap that swallowed them would leave the caller
        with a long string that says less than the short one it replaced. Empty when Ray reported
        nothing, so a caller can tell "no cause available" from "the cause is blank".
        """
        parts: list[str] = []
        if self.error_type:
            parts.append(self.error_type)
        if self.message:
            flat = " ".join(self.message.split())
            parts.append(flat if len(flat) <= message_limit else f"{flat[:message_limit]}… (truncated)")
        rendered = ": ".join(parts)
        if self.driver_exit_code is not None:
            suffix = f"driver exit {self.driver_exit_code}"
            rendered = f"{rendered} ({suffix})" if rendered else suffix
        return rendered
