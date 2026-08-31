"""A generic boto3 S3 client for any S3-compatible backend (MinIO / rustfs / AWS).

rask is storage-agnostic: the client below speaks only standard S3, so the
backend is a runtime choice (endpoint + credentials), never a code one.
`derive_hcp_creds` is an isolated, opt-in bridge for the current HCP dev backend
(a no-op for MinIO/rustfs/AWS); remove it once fully migrated off HCP.
"""

import base64
import hashlib
import os
from typing import Any


# Public alias for the boto3 S3 client. boto3 ships no public stubs, so this
# resolves to `Any` at the type level; callers get a usable name without
# reaching past storage's boundary into `mypy_boto3_s3`.
type S3Client = Any

# Env names for the S3 endpoint/insecure/CA, canonical-first. `HCP_*` are legacy
# aliases kept while HCP is the current backend (the chart + dev `.env` still set
# HCP_ENDPOINT/HCP_INSECURE); drop them once everything uses RASK_S3_*.
#
# RESOLVED BY HAND rather than by a pydantic-settings model, and the reason is dependency weight, not
# an absence of validation — there IS one coercion here, the insecure flag parsed to a bool at
# `s3_client` below, which is exactly the kind of thing a settings model would own. The surface is
# small (three optional families) and the coercion is one line, so a settings class buys uniformity
# and almost no behaviour. Weighed against that: `boto3` is this package's ONLY runtime dependency, and
# `runners/htr` + `runners/dummy` hold the package as a path dep resolved into their own sealed
# locks, so `pydantic-settings` (in neither) is a relock of two model environments for a change
# nothing can observe. The semantics also differ where it matters here: `_env_first` skips an EMPTY
# value, where `AliasChoices` stops at the first name that is SET. Callers get the precedence from
# `configured_endpoint` instead of copying this list.
_ENDPOINT_ENVS = ("RASK_S3_ENDPOINT_URL", "S3_ENDPOINT_URL", "HCP_ENDPOINT")
_INSECURE_ENVS = ("RASK_S3_INSECURE", "S3_INSECURE", "HCP_INSECURE")
_CA_BUNDLE_ENVS = ("RASK_S3_CA_BUNDLE", "S3_CA_BUNDLE")


def _env_first(names: tuple[str, ...]) -> str | None:
    """First non-empty value among `names` (canonical-first env resolution)."""
    for name in names:
        if value := os.getenv(name):
            return value
    return None


def configured_endpoint() -> str | None:
    """The deployment's OWN S3 endpoint, as `s3_client` resolves it when given none.

    Public because a caller that honours a per-run endpoint override has to know when the override
    IS the default — `RASK_S3_ENDPOINT_URL` and its two aliases are resolved in one place, and a
    second copy of that precedence list in a service is exactly the drift this returns instead.
    """
    return _env_first(_ENDPOINT_ENVS)


def derive_hcp_creds() -> dict[str, str] | None:
    """Opt-in HCP credential bridge; returns `{access_key, secret_key}` or None.

    Returns the derived pair when HCP_USERNAME/PASSWORD are set and no AWS_* key
    already wins (so MinIO/rustfs/AWS with keys set directly are untouched), else
    None. Pure — never mutates the process environment, so one process can address
    more than one backend. The current HCP dev backend issues S3 keys as
    access_key = base64(username), secret_key = md5(password) hex. `s3_client`
    applies this per client; callers need not invoke it. Legacy — drop once off HCP.
    """
    user = os.getenv("HCP_USERNAME")
    pwd = os.getenv("HCP_PASSWORD")
    if not (user and pwd):
        return None
    if os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("AWS_SECRET_ACCESS_KEY"):
        return None
    return {
        "access_key": base64.b64encode(user.encode()).decode(),
        "secret_key": hashlib.md5(pwd.encode()).hexdigest(),  # noqa: S324
    }


def s3_client(
    endpoint: str | None = None,
    *,
    access_key: str | None = None,
    secret_key: str | None = None,
    session_token: str | None = None,
    region: str | None = None,
    insecure: bool | None = None,
) -> Any:  # noqa: ANN401 — boto3 client has no public stub
    """Build a boto3 S3 client for any S3-compatible backend (MinIO / rustfs / AWS / HCP).

    Endpoint/CA/insecure flags resolve from env (RASK_S3_*/S3_*, with HCP_* as the
    legacy bridge). Path-style + s3v4 are the MinIO-safe, AWS-accepted defaults.

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
    kwargs: dict = {
        "endpoint_url": endpoint or configured_endpoint(),
        "region_name": region or os.getenv("AWS_REGION", "us-east-1"),
        "config": cfg,
    }
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
        if session_token:
            kwargs["aws_session_token"] = session_token
    elif creds := derive_hcp_creds():
        # HCP bridge applied to THIS client only — no process-global env mutation.
        kwargs["aws_access_key_id"] = creds["access_key"]
        kwargs["aws_secret_access_key"] = creds["secret_key"]
    skip_verify = insecure if insecure is not None else (_env_first(_INSECURE_ENVS) or "").lower() in ("1", "true", "yes")
    if ca := _env_first(_CA_BUNDLE_ENVS):
        kwargs["verify"] = ca
    elif skip_verify:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        kwargs["verify"] = False
    return boto3.client("s3", **kwargs)
