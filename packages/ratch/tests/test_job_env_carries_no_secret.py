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


# ── the shapes nobody had invented yet ───────────────────────────────────────────────────────────
#
# Found by the ratch/ray-kit review (2026-08-28, open_ray-kernel.md move 1): the denylist above was
# my fix for ratch-003, and it has the failure mode every denylist has — it enumerates the
# credential shapes someone thought of. `MEDIA_API_KEY` contains neither SECRET nor TOKEN nor
# PASSWORD nor CREDENTIAL, so it walked straight through into an env the Jobs API echoes. The
# medallion's mechanism (omit the secret entirely, source it from the pod) cannot have that hole,
# which is what "the two seams read identically" was supposed to buy and did not.
#
# The forward therefore inverts to FAIL-CLOSED: a name is forwarded only when its SHAPE says
# coordinate (`_URL`, `_ENDPOINT`, `_MODEL`, ...), and an unknown shape is withheld by default. That
# preserves the property the original comment defends — a new `MEDIA_FOO_URL` needs no platform
# edit — while a `MEDIA_WHATEVER_KEY` nobody anticipated is withheld rather than leaked.

LEAKY_SHAPES = {
    "MEDIA_API_KEY": "an-api-key-is-a-credential",
    "AWS_SESSION_KEY": "so-is-a-session-key",
    "MEDIA_VLLM_KEY": "and-a-vllm-key",
    "MEDIA_ADMIN_PASSPHRASE": "and-a-passphrase",
}


@pytest.mark.parametrize("name", sorted(LEAKY_SHAPES))
def test_a_credential_shape_the_denylist_never_heard_of_is_withheld(job: jobs.RunnerJob, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    env_vars = _env_vars(job, monkeypatch, {**LEAKY_SHAPES, **CONFIG})
    assert name not in env_vars, f"{name} is credential-shaped and rides the echoing Jobs API — the filter only vetoes shapes it has heard of"


def test_every_coordinate_the_jobs_actually_read_still_travels(job: jobs.RunnerJob, monkeypatch: pytest.MonkeyPatch) -> None:
    """The property the original denylist was defending, kept: the measured set of names ratch and
    the runners genuinely read — URLs, model ids, region, the access-key ID — all still forward,
    and a new `MEDIA_*_URL` needs no edit here."""
    env_vars = _env_vars(job, monkeypatch, {**LEAKY_SHAPES, **CONFIG, "MEDIA_BRAND_NEW_URL": "http://new:9999"})
    for name in (*CONFIG, "MEDIA_BRAND_NEW_URL"):
        assert name in env_vars, f"{name} is a coordinate the job needs and the fail-closed filter withheld it"
