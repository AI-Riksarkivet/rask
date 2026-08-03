"""Submit a runner's worker to a Ray cluster as a Ray Job — never by shelling out.

Mirrors lance-ns ``services/medallion/services/ray_submit.submit_stage_job`` so
the two seams read identically at merge: a DETERMINISTIC submission id per
(runner, token) makes resubmission idempotent (a second submit of the same work
re-attaches to the running job instead of racing it), the runner's own env rides
in the job's ``runtime_env``, and ``metadata={"kind": <runner>}`` labels the job
in the Ray dashboard. Differences from lance-ns, on purpose:

* **sync, SDK client** — ratch submits from a CLI (Typer), not an async Dapr
  mover, so this uses ``ray.job_submission.JobSubmissionClient`` directly
  (rask's ``ray-kit`` shape) instead of async httpx against the REST API.
* **a terminal prior job is ALWAYS deleted + resubmitted** — lance-ns re-attaches
  to a SUCCEEDED job because its trigger is an at-least-once redelivery of the
  same event; ratch's trigger is a human asking for a (re)build, so a finished
  job of any outcome means "run it again", while a still-running one means
  "attach, don't race".

When ``ray_enabled`` is off (the default — lance-ns's ``MEDALLION_RAY_ENABLED``
shape), the worker runs in-process instead: same entrypoint contract
(``runners/<name>/worker.py::main`` reading argv), no cluster needed. That only
works when the runner's deps are importable here; conflicting-env runners
(topics, kg) run locally through their sealed envs via Make targets instead.
"""

from __future__ import annotations

import importlib
import logging
import os
import shlex
import sys
import uuid
from typing import Any, Protocol

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from ratch.core.runners import runner_env, runners_root
from ratch.errors import RatchError


logger = logging.getLogger(__name__)

#: Environment prefixes forwarded into the job (store creds + service URLs) —
#: the same idea as lance-ns forwarding S3_* into its stage jobs.
_FORWARDED_ENV_PREFIXES = ("MEDIA_", "AWS_")
#: uuid5 namespace for submission ids (deterministic per (runner, token)).
_SUBMISSION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Borg93/lance-audio/runners")
_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "STOPPED"})
#: Tolerate a few transient poll blips before giving up on an in-flight job
#: (same rationale + bound as lance-ns ``_MAX_POLL_ERRORS``).
#: What the real client actually throws: the SDK raises RuntimeError for API
#: errors, but transport failures surface as requests' ConnectionError (an
#: OSError subclass) — catching RuntimeError alone lets a dead dashboard escape
#: as a raw traceback instead of the seam's RunnerJobError contract.
_CLIENT_ERRORS = (RuntimeError, OSError)


class RunnerJobError(RatchError):
    """A runner job failed to submit, failed/was stopped on the cluster, or timed out."""


class JobsSettings(BaseSettings):
    """The Ray Jobs seam config — lance-ns ``ray_enabled`` shape, ``RATCH_`` prefix."""

    model_config = SettingsConfigDict(env_prefix="RATCH_")

    ray_enabled: bool = False
    ray_address: str = "http://127.0.0.1:8265"
    ray_job_timeout_seconds: float = 3600.0
    ray_poll_interval_seconds: float = 2.0


class RunnerJob(BaseModel):
    """One runner-worker invocation: which runner, its argv, and the idempotency token."""

    model_config = {"frozen": True}

    runner: str
    entrypoint_args: tuple[str, ...] = ()
    #: Seed for the deterministic submission id — same (runner, token) re-attaches
    #: to a still-running job instead of starting a concurrent duplicate.
    token: str | None = None


class JobSubmitter(Protocol):
    """The slice of ``ray.job_submission.JobSubmissionClient`` this seam uses."""

    def submit_job(
        self,
        *,
        entrypoint: str,
        submission_id: str,
        runtime_env: dict[str, Any],
        metadata: dict[str, str],
    ) -> str: ...

    # Positional-only: Ray's real client names this param `job_id` in one method
    # and `submission_id` in another; the names must not bind the protocol.
    def get_job_status(self, submission_id: str, /) -> str: ...

    def get_job_info(self, submission_id: str, /) -> Any: ...

    def delete_job(self, submission_id: str, /) -> bool: ...


def submission_id_for(job: RunnerJob) -> str:
    """Deterministic, dashboard-legible id: ``ratch-<runner>-<uuid5(runner/token)>``."""
    seed = f"{job.runner}/{job.token or 'notoken'}"
    return f"ratch-{job.runner}-{uuid.uuid5(_SUBMISSION_NAMESPACE, seed)}"


def run_runner(
    job: RunnerJob,
    *,
    settings: JobsSettings | None = None,
    submitter: JobSubmitter | None = None,
) -> None:
    """Run a runner's worker to completion: a Ray Job when ``ray_enabled``, else in-process.

    Raises :class:`RunnerJobError` on a submit failure, a FAILED/STOPPED job, a
    timeout, or (in-process) a missing runner env / non-zero exit.
    """
    settings = settings or JobsSettings()
    if not settings.ray_enabled:
        _run_in_process(job)
        return
    submission_id = submission_id_for(job)
    client = submitter if submitter is not None else _ray_submitter(settings)
    attached = _submit_or_resubmit(client, job, submission_id)
    logger.info("runner job %s %s", submission_id, "re-attached" if attached else "submitted")
    # SUBMIT-AND-ACK (A13). The poll that stood here asked a question the data already answers: a
    # job's completion signal is its own registered commit, and the publication event off that
    # commit wakes whatever comes next. A died job commits nothing, and the lineage reconciler
    # catches it against storage truth.
    logger.info("runner job %s dispatched; completion is its registered commit", submission_id)


def _ray_submitter(settings: JobsSettings) -> JobSubmitter:
    # Imported here, not at module top: ray is heavy and `import ratch` stays light.
    from ray.job_submission import JobSubmissionClient

    try:
        return JobSubmissionClient(settings.ray_address)
    except _CLIENT_ERRORS as exc:  # the constructor pings the dashboard
        raise RunnerJobError(
            f"cannot reach the Ray dashboard at {settings.ray_address}: {exc} — start the cluster or unset RATCH_RAY_ENABLED for the in-process path"
        ) from exc


def _job_entrypoint(job: RunnerJob) -> str:
    return shlex.join(["python", "-m", f"runners.{job.runner}.worker", *job.entrypoint_args])


def _job_runtime_env(job: RunnerJob) -> dict[str, Any]:
    """working_dir (the repo, so ``runners.*`` imports resolve) + the runner's own
    pip env + forwarded store/service env vars. pip runtime_env is the DEV bridge —
    production bakes each runner into an image (docs/TODO.md)."""
    forwarded = {k: v for k, v in os.environ.items() if k.startswith(_FORWARDED_ENV_PREFIXES)}
    return {
        "working_dir": str(runners_root().parent),
        **runner_env(job.runner),
        "env_vars": forwarded,
    }


def _submit_or_resubmit(client: JobSubmitter, job: RunnerJob, submission_id: str) -> bool:
    """Submit; on a duplicate id, re-attach to a still-running job (return True) or
    delete the terminal one and resubmit fresh. See the module docstring for why
    SUCCEEDED is treated as terminal here, unlike lance-ns."""
    body = {
        "entrypoint": _job_entrypoint(job),
        "submission_id": submission_id,
        "runtime_env": _job_runtime_env(job),
        "metadata": {"kind": job.runner},
    }
    try:
        client.submit_job(**body)
        return False
    except _CLIENT_ERRORS as exc:
        try:
            status = client.get_job_status(submission_id)
        except _CLIENT_ERRORS:
            raise RunnerJobError(f"failed to submit runner job {submission_id}: {exc}") from exc
        if status not in _TERMINAL_STATUSES:
            return True
        try:
            client.delete_job(submission_id)
            client.submit_job(**body)
        except _CLIENT_ERRORS as resubmit_exc:
            raise RunnerJobError(f"failed to resubmit runner job {submission_id} (prior run {status}): {resubmit_exc}") from resubmit_exc
        return False


def _failure_message(client: JobSubmitter, submission_id: str) -> str | None:
    """The job's own failure message (lance-ns surfaces it too) — best-effort,
    a lookup error must not mask the terminal status we are about to report."""
    try:
        return getattr(client.get_job_info(submission_id), "message", None)
    except _CLIENT_ERRORS:
        return None


def _run_in_process(job: RunnerJob) -> None:
    """The ``ray_enabled=False`` fallback: import the worker and call its ``main()``.

    Same contract as the Ray entrypoint (``runners/<name>/worker.py::main`` reads
    argv), so the two paths cannot drift. Needs the runner's deps importable in
    THIS env — conflicting-env runners run through Make targets instead.
    """

    def missing_env(exc: ImportError) -> RunnerJobError:
        return RunnerJobError(
            f"runner {job.runner!r} is not importable here ({exc}) — its deps live in "
            f"runners/{job.runner}/pyproject.toml. Run it in its sealed env (see the "
            "Makefile) or set RATCH_RAY_ENABLED=1 to submit it as a Ray Job."
        )

    try:
        worker = importlib.import_module(f"runners.{job.runner}.worker")
    except ImportError as exc:
        raise missing_env(exc) from exc
    argv = [f"{job.runner}-worker", *job.entrypoint_args]
    saved_argv, sys.argv = sys.argv, argv
    try:
        exit_code = worker.main()
    except ImportError as exc:
        # Workers import their model stack lazily inside main() — a missing dep
        # there is the same "wrong env" failure as a failed module import.
        raise missing_env(exc) from exc
    finally:
        sys.argv = saved_argv
    if exit_code not in (0, None):
        raise RunnerJobError(f"runner {job.runner!r} worker exited with {exit_code}")
