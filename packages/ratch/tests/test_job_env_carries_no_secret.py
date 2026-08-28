"""No secret may ride a Ray Job's runtime_env — the Jobs API echoes it to any reader.

open_python-audit `ratch-003` — "Every AWS_* env var, including the secret access key, is copied
into the Ray Job's runtime_env". The same P0 class the medallion fixed on 2026-08-28, which did not
cross into ratch: `_FORWARDED_ENV_PREFIXES = ("MEDIA_", "AWS_")` copies every matching `os.environ`
entry into `runtime_env.env_vars`, and `GET /api/jobs/<id>` hands that dict back verbatim.

WHAT THE FORWARD IS ACTUALLY FOR, measured rather than assumed: every `MEDIA_*` name ratch reads is
a service URL or a model id (`MEDIA_EMBED_URL`, `MEDIA_SUMMARIZE_URL`, `MEDIA_CAPTION_URL`,
`MEDIA_CAPTION_MODEL`, `MEDIA_DB`) — non-secret config a job genuinely needs. Nothing in ratch or the
runners reads an `AWS_*` name at all; Lance picks those up through the implicit AWS credential chain,
which is the very chain this estate's secrets rule forbids and which `media.config.storage_options`
was rewritten to refuse.

AND REMOVING THE SECRET CANNOT BREAK THE CLUSTER, which is what makes this a deletion rather than a
migration: the Ray pods carry no `AWS_*` at all (verified against the chart and the live head), so
in-cluster this forward ships nothing. It leaks only where `AWS_*` IS set — a developer's machine or
a CI runner — and there the correct answer is not to ship the credential through an echoing API. A
job that needs S3 credentials gets them from the POD, exactly as the medallion's jobs now do.
"""

from __future__ import annotations

import pytest
from ratch.core import jobs


@pytest.fixture
def job() -> jobs.RunnerJob:
    return jobs.RunnerJob(runner="dummy", entrypoint_args=[])


SECRETS = {
    "AWS_SECRET_ACCESS_KEY": "the-developers-real-aws-secret",
    "AWS_SESSION_TOKEN": "a-session-token",
    "MEDIA_S3_SECRET_ACCESS_KEY": "the-media-plane-s3-secret",
}
CONFIG = {
    "MEDIA_EMBED_URL": "http://embed:8001",
    "MEDIA_CAPTION_MODEL": "google/gemma-4-31B-it",
    "AWS_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "an-access-key-ID-is-config-not-a-secret",
}


def _env_vars(job: jobs.RunnerJob, monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]) -> dict[str, str]:
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    return jobs._job_runtime_env(job)["env_vars"]


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_a_secret_shaped_variable_is_NEVER_forwarded(job: jobs.RunnerJob, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """The finding: the Jobs API echoes runtime_env, so anything here is readable by any caller."""
    env_vars = _env_vars(job, monkeypatch, {**SECRETS, **CONFIG})
    assert name not in env_vars, f"{name} rides the Ray Job submission, which the Jobs API echoes back"


def test_no_forwarded_VALUE_is_a_known_secret(job: jobs.RunnerJob, monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserted on values as well as names: a rename would dodge a key-only check and leak identically."""
    forwarded = " ".join(_env_vars(job, monkeypatch, {**SECRETS, **CONFIG}).values())
    for secret in SECRETS.values():
        assert secret not in forwarded, "a secret VALUE reached the submission under some other key"


@pytest.mark.parametrize("name", sorted(CONFIG))
def test_the_non_secret_config_a_job_NEEDS_still_rides(job: jobs.RunnerJob, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """The failure mode that would hide the fix: forwarding nothing also passes the tests above.

    These are the names ratch actually reads (service URLs, a model id) plus the S3 coordinates that
    are configuration — an access-key ID identifies, it does not authenticate.
    """
    assert name in _env_vars(job, monkeypatch, {**SECRETS, **CONFIG}), f"{name} was stripped with the secrets — the job cannot run without it"
