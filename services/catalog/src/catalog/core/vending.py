"""Pluggable credential vending for the catalog data plane.

The catalog authenticates (OIDC) and authorizes (OpenFGA), then a
:class:`CredentialVendor` turns *(table object-store location, access tier)* into
the ``storage_options`` a client (LanceDB SDK / lance-ray / pylance) uses to reach
object storage directly. The target is **S3-compatible** storage — RustFS (this project's default store),
MinIO, AWS S3, Ceph RGW, GCS via S3 interop. The design is
**vending-first**; each deployment picks the strongest plug it wants:

* :class:`WebIdentityVendor` — STS ``AssumeRoleWithWebIdentity`` + an inline session policy: the caller's
  OIDC id_token is exchanged BY THE STORE for short-TTL, per-table, read/write-scoped creds. The path for
  **RustFS** (it trusts the OIDC issuer but does NOT support plain ``AssumeRole``). Token-authenticated.
* :class:`StsVendor` — STS ``AssumeRole`` + an inline session policy: short-TTL,
  per-table, read/write-scoped tokens. For backends that implement plain ``AssumeRole``
  (AWS, MinIO, Ceph RGW) — NOT RustFS.
* :class:`StaticPrefixVendor` — hands out a pre-provisioned per-bucket key
  (long-lived). For simple setups (e.g. a static MinIO/HMAC key, or GCS interop)
  where direct client I/O is wanted but STS isn't configured.
* :class:`ModeBVendor` — ``vend`` returns ``None``: no credential ever leaves the
  catalog; the client uses the server-mediated (Arrow-IPC) data endpoints. The
  simplest, backend-agnostic default — nothing is delegated.

OpenFGA decides the tier: ``can_read_data`` -> ``"read"``, ``can_write_data`` ->
``"write"``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Literal, Protocol, assert_never, cast, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel


Tier = Literal["read", "write"]
VendingMode = Literal["mode_b", "static", "sts", "web_identity"]


class VendedCredentials(BaseModel):
    """Scoped storage credentials for one table at one tier.

    ``storage_options`` is consumed directly by pylance / lance-ray /
    object_store. ``expires_at_millis`` is when the client must refresh
    (``None`` for long-lived static keys).
    """

    storage_options: dict[str, str]
    expires_at_millis: int | None = None


@runtime_checkable
class CredentialVendor(Protocol):
    """Vend scoped storage credentials for one table prefix at one tier."""

    def vend(self, *, table_location: str, tier: Tier, web_identity_token: str | None = None) -> VendedCredentials | None:
        """Return creds for ``table_location`` at ``tier``.

        ``web_identity_token`` is the caller's OIDC JWT — used ONLY by :class:`WebIdentityVendor` (the store
        exchanges the token for creds); other vendors ignore it. ``None`` means "no direct credential" — the
        caller falls back to the server-mediated (Mode B) data path.
        """
        ...


def split_s3_location(location: str) -> tuple[str, str]:
    """Return ``(bucket, key_prefix)`` for an ``s3://bucket/key...`` location.

    Raises:
        ValueError: if ``location`` has no bucket (authority) component.
    """
    parts = urlsplit(location)
    bucket = parts.netloc
    if not bucket:
        raise ValueError(f"location has no bucket: {location!r}")
    return bucket, parts.path.lstrip("/")


_READ_ACTIONS = ("s3:GetObject",)
_WRITE_ACTIONS = (
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:AbortMultipartUpload",
)

# Default role ARN for STS vending. RustFS ignores it (it authorizes by the OIDC token + ROLE_POLICY); AWS /
# MinIO / Ceph resolve it. boto3 requires the param either way. Override via LANCE_S3_ASSUME_ROLE_ARN.
_DEFAULT_VEND_ROLE_ARN = "arn:aws:iam::000000000000:role/lance-vend"


def build_session_policy(bucket: str, prefix: str, tier: Tier) -> dict[str, object]:
    """Build an STS inline session policy scoping access to one table prefix + tier.

    Two statements: ``s3:ListBucket`` on the bucket gated by an ``s3:prefix``
    condition, plus object actions on ``bucket/<prefix>/*``. Read tier =
    GET + List; write tier additionally allows PUT / DELETE /
    AbortMultipartUpload. As an STS *session* policy this can only RESTRICT the
    catalog's role (intersection-only), never widen it.

    Raises:
        ValueError: if ``prefix`` carries an IAM wildcard metachar (``*``/``?``). These are wildcards
            inside a Resource ARN and an ``s3:prefix`` condition with no way to escape them, so a prefix
            holding one would widen the grant to siblings. The create doors already reject such a segment
            (``identifiers.require_safe_segments``); this is the belt behind that suspenders.
    """
    if any(c in prefix for c in ("*", "?")):
        raise ValueError(f"prefix {prefix!r} contains an IAM wildcard metacharacter ('*'/'?'); it would widen the vended policy to sibling objects")
    prefix = prefix.rstrip("/")
    obj_actions = list(_WRITE_ACTIONS if tier == "write" else _READ_ACTIONS)
    list_prefixes = [f"{prefix}/*"] if prefix else ["*"]
    obj_resource = f"arn:aws:s3:::{bucket}/{prefix}/*" if prefix else f"arn:aws:s3:::{bucket}/*"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ListTablePrefix",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {"StringLike": {"s3:prefix": list_prefixes}},
            },
            {
                "Sid": "TableObjects",
                "Effect": "Allow",
                "Action": obj_actions,
                "Resource": obj_resource,
            },
        ],
    }


class ModeBVendor:
    """No vending: data flows through the catalog's server-mediated endpoints."""

    def vend(self, *, table_location: str, tier: Tier, web_identity_token: str | None = None) -> VendedCredentials | None:
        return None


class StaticPrefixVendor:
    """Hand out pre-provisioned per-bucket ``storage_options`` (long-lived keys).

    ``keys_by_bucket`` maps a bucket name to the ``storage_options`` for that
    bucket (typically loaded from OpenBao). Returns ``None`` for an unknown
    bucket so the caller falls back to Mode B rather than vending nothing useful.

    For S3-compatible stores where you provision a dedicated, rotatable,
    least-privilege key per bucket (e.g. a static MinIO/HMAC key or GCS interop
    key). Prefer :class:`StsVendor` when the backend supports STS — static keys
    are long-lived and can't be scoped per-table the way a session policy can.
    """

    def __init__(self, keys_by_bucket: dict[str, dict[str, str]]) -> None:
        self._keys = keys_by_bucket

    def vend(self, *, table_location: str, tier: Tier, web_identity_token: str | None = None) -> VendedCredentials | None:
        bucket, _ = split_s3_location(table_location)
        opts = self._keys.get(bucket)
        if opts is None:
            return None
        return VendedCredentials(storage_options=dict(opts), expires_at_millis=None)


def _expiry_millis(expiration: object, ttl_seconds: int) -> int:
    """Epoch millis for an STS ``Expiration`` (a datetime), or now + ttl."""
    ts = getattr(expiration, "timestamp", None)
    if callable(ts):
        return int(ts() * 1000)
    return int((time.time() + ttl_seconds) * 1000)


class StsVendor:
    """STS ``AssumeRole`` + inline session policy → short-TTL, per-table creds.

    The gold-standard plug for S3-family stores that implement the plain ``AssumeRole`` flow — AWS, MinIO,
    Ceph RGW, moto **and RustFS**, this project's default store.

    RUSTFS ACCEPTS IT, MEASURED. `AssumeRole` with an inline session policy against the live RustFS STS
    endpoint returns a session token (2026-08-30, from the Ray head, boto3 with the tenant credential),
    and the policy is ENFORCED: in-scope list/GET allowed, an out-of-scope prefix and bucket refused 403,
    a read-tier PUT refused, a write-tier in-scope PUT allowed. An UNSIGNED probe fails with
    ``InvalidRequest`` because RustFS verifies STS requests as SigV4-signed ``s3`` — which is a statement
    about signing, not about the operation being unsupported, and reading it as the latter is what put
    this plug out of reach.

    That matters for which mode can serve which caller. ``web_identity`` requires the CALLER to present an
    OIDC token, so it cannot serve the cascade at all: a mover authenticates with ``dapr-api-token`` +
    ``x-lance-service-identity`` and never holds a bearer, so the vend returns ``None``. This plug has no
    such requirement.

    ``assume_role`` defaults to a lazily-built boto3 STS client's ``assume_role`` and is injectable for
    tests.
    """

    def __init__(
        self,
        *,
        role_arn: str,
        region: str,
        endpoint: str | None = None,
        ttl_seconds: int = 900,
        assume_role: Callable[..., dict[str, object]] | None = None,
    ) -> None:
        self._role_arn = role_arn
        self._region = region
        self._endpoint = endpoint
        self._ttl = ttl_seconds
        self._assume_role = assume_role or self._default_assume_role
        self._client: Any = None  # boto3 STS client, built once (the vendor is a lifespan singleton)

    def _default_assume_role(self, **kwargs: object) -> dict[str, object]:
        if self._client is None:
            import boto3  # lazy: only needed when STS vending is actually enabled

            self._client = boto3.client("sts", region_name=self._region, endpoint_url=self._endpoint)
        return cast(dict[str, object], self._client.assume_role(**kwargs))

    def vend(self, *, table_location: str, tier: Tier, web_identity_token: str | None = None) -> VendedCredentials | None:
        bucket, prefix = split_s3_location(table_location)
        policy = build_session_policy(bucket, prefix, tier)
        resp = self._assume_role(
            RoleArn=self._role_arn,
            RoleSessionName="lance-catalog-vend",
            Policy=json.dumps(policy),
            DurationSeconds=self._ttl,
        )
        creds = cast(dict[str, object], resp["Credentials"])
        opts: dict[str, str] = {
            "access_key_id": str(creds["AccessKeyId"]),
            "secret_access_key": str(creds["SecretAccessKey"]),
            "session_token": str(creds["SessionToken"]),
            "region": self._region,
        }
        if self._endpoint:
            opts["endpoint"] = self._endpoint
        return VendedCredentials(
            storage_options=opts,
            expires_at_millis=_expiry_millis(creds.get("Expiration"), self._ttl),
        )


class WebIdentityVendor:
    """STS ``AssumeRoleWithWebIdentity`` — the caller's OIDC JWT (e.g. a Dex id_token) is exchanged BY THE
    STORE for short-TTL creds bound to the provider's ``ROLE_POLICY``, which the inline session policy then
    narrows per-table. The native flow for RustFS (it trusts the OIDC issuer and does NOT support plain
    ``AssumeRole``). Token-authenticated, so — unlike ``AssumeRole`` — it needs no SigV4-signed catalog creds
    (RustFS verifies the JWT, not a request signature); the request goes out UNSIGNED. ``assume`` is the
    boto3 ``assume_role_with_web_identity`` and is injectable for tests.
    """

    def __init__(
        self,
        *,
        region: str,
        endpoint: str | None = None,
        role_arn: str = _DEFAULT_VEND_ROLE_ARN,
        ttl_seconds: int = 900,
        assume: Callable[..., dict[str, object]] | None = None,
    ) -> None:
        self._region = region
        self._endpoint = endpoint
        self._role_arn = role_arn  # RustFS ignores it; boto3 requires the param
        self._ttl = ttl_seconds
        self._assume = assume or self._default_assume
        self._client: Any = None  # boto3 STS client, built once (the vendor is a lifespan singleton)

    def _default_assume(self, **kwargs: object) -> dict[str, object]:
        if self._client is None:
            import boto3  # lazy: only when web_identity vending is enabled
            from botocore import UNSIGNED
            from botocore.config import Config

            self._client = boto3.client(
                "sts",
                region_name=self._region,
                endpoint_url=self._endpoint,
                config=Config(signature_version=UNSIGNED),  # token-authenticated, not SigV4
            )
        return cast(dict[str, object], self._client.assume_role_with_web_identity(**kwargs))

    def vend(self, *, table_location: str, tier: Tier, web_identity_token: str | None = None) -> VendedCredentials | None:
        if not web_identity_token:  # no caller token to exchange → fall back to server-mediated
            return None
        bucket, prefix = split_s3_location(table_location)
        resp = self._assume(
            RoleArn=self._role_arn,
            RoleSessionName="lance-catalog-vend",
            WebIdentityToken=web_identity_token,
            Policy=json.dumps(build_session_policy(bucket, prefix, tier)),
            DurationSeconds=self._ttl,
        )
        creds = cast(dict[str, object], resp["Credentials"])
        opts: dict[str, str] = {
            "access_key_id": str(creds["AccessKeyId"]),
            "secret_access_key": str(creds["SecretAccessKey"]),
            "session_token": str(creds["SessionToken"]),
            "region": self._region,
        }
        if self._endpoint:
            opts["endpoint"] = self._endpoint
        return VendedCredentials(storage_options=opts, expires_at_millis=_expiry_millis(creds.get("Expiration"), self._ttl))


def make_vendor(
    mode: VendingMode,
    *,
    region: str = "us-east-1",
    sts_endpoint: str | None = None,
    assume_role_arn: str | None = None,
    ttl_seconds: int = 900,
    static_keys: dict[str, dict[str, str]] | None = None,
) -> CredentialVendor:
    """Build the configured :class:`CredentialVendor`.

    Raises:
        ValueError: for ``sts`` mode without ``assume_role_arn``, or an unknown mode.
    """
    if mode == "mode_b":
        return ModeBVendor()
    if mode == "static":
        return StaticPrefixVendor(static_keys or {})
    if mode == "sts":
        if not assume_role_arn:
            raise ValueError("assume_role_arn is required for sts vending mode")
        return StsVendor(
            role_arn=assume_role_arn,
            region=region,
            endpoint=sts_endpoint,
            ttl_seconds=ttl_seconds,
        )
    if mode == "web_identity":
        return WebIdentityVendor(
            region=region,
            endpoint=sts_endpoint,
            role_arn=assume_role_arn or _DEFAULT_VEND_ROLE_ARN,
            ttl_seconds=ttl_seconds,
        )
    assert_never(mode)


def has_external_bases(location: str, storage_options: dict[str, str]) -> bool:
    """True if the table's data physically lives in registered NON-root bases (#3-B multi-base) — any data
    file with a ``base_id`` set. Such a table cannot be safely direct-vended: the STS session policy is scoped
    to the primary root bucket only, so a data-base fragment would be denied at the object store. Short-
    circuits on the first external file; only called when the multi-base feature is enabled.

    A VENDING question, which is why it lives here rather than in an endpoint module: both the vend
    door (``credentials.py``) and describe-with-vending (``tables.py``) must answer it identically,
    and a helper shared across endpoint modules belongs in the layer they both already import.
    """
    import lance  # lazy, matching this module's boto3 style: pylance loads only where vending runs

    try:
        ds = lance.dataset(location, storage_options=storage_options)
        return any(getattr(df, "base_id", None) for frag in ds.get_fragments() for df in frag.data_files())
    except (ValueError, OSError):
        return False
