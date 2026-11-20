"""A generic boto3 S3 client for any S3-compatible backend (RustFS / AWS / any S3).

rask is storage-agnostic: the client below speaks only standard S3, so the
backend is a runtime choice (endpoint + credentials), never a code one. It derives no
credential of its own and reads none from its surroundings: every client signs with the key pair its caller passes, and
a caller with none gets no client ([[XC-219]]).
"""

from __future__ import annotations

import os
from functools import cache
from typing import TYPE_CHECKING, Any, Final, NotRequired, TypedDict

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakehouse.storage.credential import S3KeyPair
from lakehouse.storage.errors import MissingStoreCredentialError


if TYPE_CHECKING:
    from boto3.session import Session
    from mypy_boto3_s3.client import S3Client as _BotoS3Client


#: The boto3 S3 client, as `boto3-stubs[s3]` types it: callers get a usable name without reaching past storage's
#: boundary into `mypy_boto3_s3`, which is a type-only dependency.
type S3Client = _BotoS3Client

#: The most requests one client has in flight at once, and so the connections its pool keeps: the widest fan-out that
#: shares a client, `lakehouse.storage.stored_objects.identities`. botocore's default pool keeps ten, so sixteen reads closed six
#: connections and opened them again on the next batch (measured on the dev estate 2026-11-08: urllib3's `Connection
#: pool is full, discarding connection` on every sweep tick).
MAX_POOL_CONNECTIONS: Final = 16


_TRUE_FLAGS = ("1", "true", "yes")


class S3EnvSettings(BaseSettings):
    """The deployment's own S3 endpoint and TLS posture, as the environment states them.

    With `env_ignore_empty`, a name set to the empty string counts as unset. Built per client, so a client reads the
    environment as it is when the client is made.
    """

    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore", env_ignore_empty=True)

    endpoint: str | None = Field(default=None, validation_alias="RASK_S3_ENDPOINT_URL")
    insecure: bool = Field(default=False, validation_alias="RASK_S3_INSECURE")
    ca_bundle: str | None = Field(default=None, validation_alias="RASK_S3_CA_BUNDLE")

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


def s3_client(endpoint: str | None = None, *, key_pair: S3KeyPair, region: str | None = None, insecure: bool | None = None) -> S3Client:
    """Build a boto3 S3 client for any S3-compatible backend (RustFS / AWS / any S3), signed by ``key_pair``.

    Endpoint/CA/insecure flags resolve from env (`RASK_S3_*`). Path-style + s3v4 are the RustFS-safe, AWS-accepted defaults.

    ``key_pair`` is the only credential the client signs with (:func:`_signing`). ``region`` and ``insecure`` override
    the env for THIS client only: the estate spans more than one store (the governed tables on the deployment's own, a
    warehouse on another), and reading a store from the wrong backend does not error, it returns an empty listing, which
    is the least debuggable failure available. ``region`` omitted → ``AWS_REGION``, default us-east-1.

    Raises:
        TypeError: ``key_pair`` is not an :class:`~lakehouse.storage.credential.S3KeyPair`.
        MissingStoreCredentialError: ``key_pair`` names no key id or no secret.
    """
    env = S3EnvSettings()
    return _client(endpoint or env.endpoint, env, key_pair=key_pair, region=region, insecure=insecure)


def aws_s3_client(*, key_pair: S3KeyPair, region: str) -> S3Client:
    """A boto3 S3 client for AWS proper in ``region``, signed by ``key_pair``: the client for a root whose storage
    options name no endpoint.

    It addresses the endpoint botocore resolves for the region and no other: neither the deployment's own
    (``RASK_S3_ENDPOINT_URL``) nor one the environment or a shared config file names (``AWS_ENDPOINT_URL``,
    ``AWS_ENDPOINT_URL_S3``, ``endpoint_url``), any of which would be handed the pair and the bytes ([[LH-572]]).

    Raises:
        TypeError: ``key_pair`` is not an :class:`~lakehouse.storage.credential.S3KeyPair`.
        MissingStoreCredentialError: ``key_pair`` names no key id or no secret.
    """
    return _client(None, S3EnvSettings(), key_pair=key_pair, region=region, insecure=None)


@cache
def aws_session() -> Session:
    """The one boto3 session every client of this package is built from: it reads no profile and no shared file.

    boto3's default session takes ``AWS_PROFILE``/``AWS_DEFAULT_PROFILE``, ``AWS_CONFIG_FILE`` and
    ``AWS_SHARED_CREDENTIALS_FILE`` from the environment, so a profile name the environment holds decides whether a
    client can be built at all (``ProfileNotFound``, measured on botocore 1.43.58 with an explicit key pair passed), and
    a config file it names applies its settings to every client. These three are pinned here to no profile and no file.

    Built once per process: a client costs about 1.6 ms from a built session and about 35 ms from a fresh one (measured
    on botocore 1.43.58), and every read of the store builds one. Shared across threads as boto3's default session is.
    """
    import boto3
    from botocore.session import Session as BotocoreSession

    # botocore's `session_vars` entries are (config file key, environment variables, default, converter).
    unread = {
        "profile": (None, None, None, None),
        "config_file": (None, None, os.devnull, None),
        "credentials_file": (None, None, os.devnull, None),
    }
    return boto3.session.Session(botocore_session=BotocoreSession(session_vars=unread))


class _Signing(TypedDict):
    """The credential keywords a boto3 client is built with."""

    aws_access_key_id: str
    aws_secret_access_key: str
    aws_session_token: NotRequired[str]


def _signing(key_pair: S3KeyPair) -> _Signing:
    """The credential arguments a client is built with: ``key_pair`` and nothing else.

    botocore uses its default chain (environment variables, shared files, profiles, a credential process, an assumed
    role, container and instance metadata) whenever a client is built without both halves of a pair, including when it
    is given a lone session token, and signs with whatever that finds. So a pair missing a half builds no client.

    Raises:
        TypeError: ``key_pair`` is not an :class:`~lakehouse.storage.credential.S3KeyPair`.
        MissingStoreCredentialError: ``key_pair`` names no key id or no secret.
    """
    if not isinstance(key_pair, S3KeyPair):
        raise TypeError(f"a store client signs with an S3KeyPair, not {type(key_pair).__name__}")
    secret = key_pair.secret_access_key.get_secret_value()
    if missing := [name for name, value in (("access_key_id", key_pair.access_key_id), ("secret_access_key", secret)) if not value]:
        raise MissingStoreCredentialError(f"the key pair names no {' and no '.join(missing)}, so no store client is built")
    signing: _Signing = {"aws_access_key_id": key_pair.access_key_id, "aws_secret_access_key": secret}
    if key_pair.session_token is not None and (token := key_pair.session_token.get_secret_value()):
        signing["aws_session_token"] = token
    return signing


def _client(endpoint: str | None, env: S3EnvSettings, *, key_pair: S3KeyPair, region: str | None, insecure: bool | None) -> S3Client:
    """The boto3 client both builders hand out; ``endpoint`` None is the region's own, whatever the environment names."""
    from botocore.config import Config

    signing = _signing(key_pair)
    cfg = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        retries={"max_attempts": 3, "mode": "adaptive"},
        connect_timeout=10,
        read_timeout=60,
        max_pool_connections=MAX_POOL_CONNECTIONS,
    )
    if endpoint is None:
        # botocore then resolves the region's own endpoint whatever `AWS_ENDPOINT_URL`, `AWS_ENDPOINT_URL_S3` or a shared
        # config file names. Its type stub does not list the option, so this one key is passed as a map.
        unlisted: dict[str, Any] = {"ignore_configured_endpoint_urls": True}
        cfg = cfg.merge(Config(**unlisted))
    verify: str | bool | None = None
    skip_verify = insecure if insecure is not None else env.insecure
    if ca := env.ca_bundle:
        verify = ca
    elif skip_verify:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        verify = False
    return aws_session().client("s3", endpoint_url=endpoint, region_name=region or AwsRegionSettings().aws_region, config=cfg, verify=verify, **signing)


__all__ = [
    "MAX_POOL_CONNECTIONS",
    "AwsRegionSettings",
    "S3Client",
    "S3EnvSettings",
    "aws_s3_client",
    "aws_session",
    "configured_endpoint",
    "s3_client",
]
