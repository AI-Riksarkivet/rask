"""A generic boto3 S3 client for any S3-compatible backend (RustFS / AWS / any S3).

rask is storage-agnostic: the client below speaks only standard S3, so the
backend is a runtime choice (endpoint + credentials), never a code one. It derives no
credential of its own: a key pair is passed per client or comes from the standard AWS chain.
"""

from typing import Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Public alias for the boto3 S3 client. boto3 ships no public stubs, so this
# resolves to `Any` at the type level; callers get a usable name without
# reaching past storage's boundary into `mypy_boto3_s3`.
type S3Client = Any


_TRUE_FLAGS = ("1", "true", "yes")


class S3EnvSettings(BaseSettings):
    """The deployment's own S3 endpoint and TLS posture, as the environment states them.

    Each family is read canonical-first (`RASK_S3_*`, then `S3_*`, then the `HCP_*` names `.env.example` and
    `scripts/smoke_s3.py` use). With `env_ignore_empty`, a name set to the empty string counts as unset and
    `AliasChoices` goes on to the next name. Built per client, so a client reads the environment as it is when the
    client is made.
    """

    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore", env_ignore_empty=True)

    endpoint: str | None = Field(default=None, validation_alias=AliasChoices("RASK_S3_ENDPOINT_URL", "S3_ENDPOINT_URL", "HCP_ENDPOINT"))
    insecure: bool = Field(default=False, validation_alias=AliasChoices("RASK_S3_INSECURE", "S3_INSECURE", "HCP_INSECURE"))
    ca_bundle: str | None = Field(default=None, validation_alias=AliasChoices("RASK_S3_CA_BUNDLE", "S3_CA_BUNDLE"))

    @field_validator("insecure", mode="before")
    @classmethod
    def _only_a_true_word_skips_verification(cls, value: object) -> object:
        """Only `1`, `true` or `yes` (any case) turns verification off; any other word leaves it on rather than failing the client."""
        return value.lower() in _TRUE_FLAGS if isinstance(value, str) else value


class AwsRegionSettings(BaseSettings):
    """`AWS_REGION`, the region a client signs for when its caller names none.

    Kept apart from `S3EnvSettings` because here an empty variable is a value, not an absence: `AWS_REGION=""` reaches
    boto3 as `""`, as the standard AWS chain reads it.
    """

    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore")

    aws_region: str = Field(default="us-east-1", validation_alias="AWS_REGION")


def configured_endpoint() -> str | None:
    """The deployment's OWN S3 endpoint, as `s3_client` resolves it when given none.

    Public because a caller that honours a per-run endpoint override has to know when the override
    IS the default — `RASK_S3_ENDPOINT_URL` and its two aliases are resolved in one place, and a
    second copy of that precedence list in a service is exactly the drift this returns instead.
    """
    return S3EnvSettings().endpoint


def s3_client(
    endpoint: str | None = None,
    *,
    access_key: str | None = None,
    secret_key: str | None = None,
    session_token: str | None = None,
    region: str | None = None,
    insecure: bool | None = None,
) -> Any:  # noqa: ANN401 — boto3 client has no public stub
    """Build a boto3 S3 client for any S3-compatible backend (RustFS / AWS / any S3).

    Endpoint/CA/insecure flags resolve from env (RASK_S3_*/S3_*, then the HCP_* aliases). Path-style + s3v4 are the RustFS-safe, AWS-accepted defaults.

    ``access_key``/``secret_key``/``session_token``/``region``/``insecure`` override the env for THIS
    client only. Env-only credentials are a single global pair, so one process could address exactly
    one backend — and the estate genuinely spans two: the governed tiers on the deployment's own
    store, and raw on an external one with different keys. Reading a store from the wrong backend
    does not error, it returns an empty listing, which is the least debuggable failure available.
    Passing them per call is what lets one process serve both. Omitted → the env chain.

    ``region`` is per-client for the same reason: a caller holding a store's OWN region (a warehouse
    record, a lance-style ``storage_options`` dict) must be able to spend it, or every backend in the
    process is pinned to whatever ``AWS_REGION`` says. Omitted → ``AWS_REGION``, default us-east-1.

    ``session_token`` completes a vended temporary credential. It is refused without its key pair
    because botocore silently IGNORES a lone token and falls back to the env credential chain — the
    request then signs as a different identity and the store's 403 names the wrong problem.
    """
    import boto3
    from botocore.config import Config

    cfg = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        retries={"max_attempts": 3, "mode": "adaptive"},
        connect_timeout=10,
        read_timeout=60,
    )
    if session_token and not (access_key and secret_key):
        raise ValueError("session_token needs access_key and secret_key: botocore drops a lone token and signs with the env credential chain instead")
    env = S3EnvSettings()
    kwargs: dict = {
        "endpoint_url": endpoint or env.endpoint,
        "region_name": region or AwsRegionSettings().aws_region,
        "config": cfg,
    }
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
        if session_token:
            kwargs["aws_session_token"] = session_token
    skip_verify = insecure if insecure is not None else env.insecure
    if ca := env.ca_bundle:
        kwargs["verify"] = ca
    elif skip_verify:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        kwargs["verify"] = False
    return boto3.client("s3", **kwargs)
