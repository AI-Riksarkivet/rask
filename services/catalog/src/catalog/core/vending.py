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
* :class:`ModeBVendor` — ``vend`` returns ``None``: no credential ever leaves the
  catalog; the client uses the server-mediated (Arrow-IPC) data endpoints. The
  simplest, backend-agnostic default — nothing is delegated.

OpenFGA decides the tier: ``can_read_data`` -> ``"read"``, ``can_write_data`` ->
``"write"``.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol, assert_never, cast, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel

from catalog.core.config import shared_lance_session
from service_kit.lakehouse.objectfs import lance_storage_options


log = logging.getLogger(__name__)

Tier = Literal["read", "write"]
VendingMode = Literal["mode_b", "sts", "web_identity"]


class VendedCredentials(BaseModel):
    """Scoped storage credentials for one table at one tier.

    ``storage_options`` is consumed directly by pylance / lance-ray /
    object_store. ``expires_at_millis`` is when the client must refresh
    (``None`` for long-lived static keys).
    """

    storage_options: dict[str, str]
    expires_at_millis: int | None = None


class EncryptionAtRest(BaseModel):
    """What a vended credential must tell the writer about encrypting the bytes it lands (§ J5).

    THE POINT IS THE DIRECT WRITE. A client vended credentials writes to object storage without the
    catalog in the path, so any encryption the warehouse intends has to travel IN the storage options
    or it does not happen. Key names and their contract are verified against `lance_docs/guide.md`
    (2417-2419), not guessed — `lance_storage_options` refuses a combination the store would ignore.

    Empty is the default and means "whatever the bucket does", which is what every deployment had
    before this existed: unset here changes nothing.
    """

    algorithm: str | None = None
    kms_key_id: str | None = None
    bucket_key_enabled: bool | None = None

    def as_options(self) -> dict[str, object]:
        """The keyword arguments `lance_storage_options` takes — one place, so a rename cannot half-land."""
        return {
            "server_side_encryption": self.algorithm,
            "sse_kms_key_id": self.kms_key_id,
            "sse_bucket_key_enabled": self.bucket_key_enabled,
        }


@runtime_checkable
class CredentialVendor(Protocol):
    """Vend scoped storage credentials for one table prefix at one tier."""

    def vend(
        self, *, table_location: str, tier: Tier, web_identity_token: str | None = None, bases: Sequence[str] = (), branch: str = ""
    ) -> VendedCredentials | None:
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


def _reject_iam_metacharacters(what: str, value: str) -> None:
    """``*`` and ``?`` are wildcards inside a Resource ARN and an ``s3:prefix`` condition, with no escape.

    A value carrying one widens the grant to siblings, so both the table prefix and every base path go
    through here. The base path needs it MORE: a prefix comes off the create doors, which already ran
    ``identifiers.require_safe_segments``, while a base path comes off a MANIFEST.
    """
    if any(c in value for c in ("*", "?")):
        raise ValueError(f"{what} {value!r} contains an IAM wildcard metacharacter ('*'/'?'); it would widen the vended policy to sibling objects")


def _reject_a_base_that_is_not_a_location(base: str, base_prefix: str) -> None:
    """A base must name a place INSIDE a bucket, never the bucket itself.

    With no prefix the base's object statement collapses to ``arn:aws:s3:::<bucket>/*`` — measured
    2026-09-11, a base of ``s3://lakehouse`` grants READ on the whole lakehouse bucket and a base of
    ``s3://rask-observability`` grants READ on a bucket the table has nothing to do with. One declared
    base would turn a credential scoped to a single table prefix into a bucket-wide reader.

    THE INPUT IS CHOSEN BY A WRITER, which is why depth has to be checked rather than assumed. Bases are
    read from the table's own manifest, and a write-tier vend grants ``PutObject`` on ``<prefix>/*``,
    which covers ``_versions/`` — enough to commit a manifest client-side. So the value here is one a
    writer on ONE table can pick, used to widen that same writer's next credential.
    :func:`_reject_iam_metacharacters` already treats this field as untrusted for wildcards; this is the
    other half of the same distrust.

    REFUSING A BUCKET ROOT CANNOT NARROW A REAL TABLE: the spec's base path points at a dataset root or
    a file directory (``file_format.md``, Base Path System), never at a bucket. A base in its OWN bucket
    stays legitimate — containment here is about DEPTH, not about the bucket matching the table's.

    ``..`` is refused on the reasoning ``uri_within`` already records: no location the catalog vends
    contains one, so its presence is evidence the value was not vended.
    """
    if not base_prefix:
        raise ValueError(
            f"base path {base!r} names a bucket root, so granting it would widen this credential to the whole bucket; "
            f"a base points at a dataset root or a file directory, never at a bucket"
        )
    if ".." in base_prefix.split("/"):
        raise ValueError(f"base path {base!r} contains a '..' segment; no location the catalog vends contains one")


def _location_within(outer: tuple[str, str], inner: tuple[str, str]) -> bool:
    """True iff ``inner`` names a location inside ``outer`` — same bucket, at or under its key prefix.

    Containment is tested against ``<prefix>/`` rather than as a bare string prefix, and the bucket must
    match exactly: otherwise ``acme-wh/mine$t-evil`` passes for ``acme-wh/mine$t`` and the bucket
    ``lakehouse-evil`` passes for ``lakehouse``. Both near-misses are reachable by a writer choosing a
    name, which is the input this whole loop distrusts.

    An empty outer prefix IS the whole bucket — the policy renders ``<bucket>/*`` for that table — so
    same-bucket is containment there. It cannot arrive from a base:
    :func:`_reject_a_base_that_is_not_a_location` refuses a bucket root before this is asked.
    """
    outer_bucket, outer_prefix = outer
    inner_bucket, inner_prefix = inner
    if inner_bucket != outer_bucket:
        return False
    stem = outer_prefix.rstrip("/")
    if not stem:
        return True
    return inner_prefix == stem or inner_prefix.startswith(f"{stem}/")


def _base_is_sanctioned(base: tuple[str, str], table: tuple[str, str], sanctioned_bases: Sequence[str]) -> bool:
    """May this declared base be granted READ on the caller's credential?

    TWO SANCTIONS, and neither is a shape rule. A base inside the table's OWN vended scope adds nothing
    the credential does not already carry — that is what a shallow clone and a branch are, and measured
    2026-09-13 it is the shape live tables declare (``<table-root>/tree/work``). A base outside it is
    legitimate only because an OPERATOR said so: ``LANCE_MULTIBASE_DATA_BASES`` is the estate's existing
    allowlist for exactly this, and its own config note states the rule — "a caller can never point a
    base at an arbitrary bucket (data-exfil / rogue-write door)". The create door enforced that list and
    the vend door did not, which is the asymmetry this closes.

    The alternative considered and rejected was resolving each base to a catalog table and checking the
    caller's read rung on it. There is no location->table index, so that costs either a walk of the
    estate on a 900 s-TTL hot path or a reversal of the backend's own layout convention. The operator's
    allowlist answers the same question — is this foreign location a legitimate part of this lakehouse —
    without either.
    """
    if _location_within(table, base):
        return True
    return any(_location_within(split_s3_location(entry), base) for entry in sanctioned_bases)


def unsanctioned_bases(table_location: str, bases: Sequence[str], sanctioned_bases: Sequence[str] = ()) -> tuple[str, ...]:
    """The declared bases a vended session policy would NOT be able to grant.

    [[LH-057]]. The vend door refuses a direct credential to any multi-base table, which was right
    while the policy was scoped to the primary bucket alone: a client would be denied at the object
    store on the first fragment that lived elsewhere. The policy grants every sanctioned base now
    (:func:`build_session_policy`), so the question is no longer "does this table have bases" but
    "is there a base this credential could not reach" — and only the second justifies proxying the
    table through the catalog's root credential.

    Empty answer = direct-vendable. A table declaring no bases, which is the overwhelming majority,
    costs nothing here.

    THE DECLARED LIST SUPERSEDES THE FRAGMENT SCAN THIS REPLACED, and it is the safe direction:
    ``base_id`` INDEXES ``base_paths``, so every base a fragment resolves through is in the manifest's
    declared list, and "every declared base is sanctioned" implies every fragment is covered. It also
    retires a trap the scan carried — the first registered base is ``0`` while a file under the
    dataset's own root reads ``None``, so a truthy test called those two identical and answered "no
    external bases" for the very shapes it was written to catch (a shallow clone and a branch, measured
    on pylance 10.0.0). Reading the manifest asks no such question.

    THE SAME PREDICATE THE POLICY USES, deliberately: :func:`_base_is_sanctioned` decides what
    ``build_session_policy`` grants, so asking anything else would let the door and the policy disagree
    about the same base — the door proxying what the policy would have covered, or direct-vending what
    it would have dropped.
    """
    return tuple(base for base in bases if not _covered(table_location, base, sanctioned_bases))


def _covered(table_location: str, base: str, sanctioned_bases: Sequence[str]) -> bool:
    """Would the policy grant this one base? A location it cannot SPLIT is one it must not vouch for.

    ``split_s3_location`` raises on anything that is not ``s3://bucket/key`` — a local ``dir``-backend
    path, a spelling this module does not know. The policy is written in S3 ARNs, so a base it cannot
    address is a base a direct client could not reach, and the honest answer is the fallback rather than
    an exception out of the vend door. Fail-closed, the same direction ``sanctioned_bases`` defaults in.
    """
    try:
        return _base_is_sanctioned(split_s3_location(base), split_s3_location(table_location), sanctioned_bases)
    except ValueError:
        return False


def dataset_facts(location: str, storage_options: dict[str, str]) -> tuple[int, tuple[str, ...]]:
    """``(current version, declared base paths)`` from ONE root-cred manifest read.

    The version is the client's optimistic-append base; 0 for a declared-only/new table with no
    readable dataset yet. The bases are what the vended policy must also be able to READ — a table
    whose fragments carry a ``base_id`` resolves them through those paths, so a credential scoped to
    the table prefix alone is scoped to less than the table is (§ H12, measured: 69 datasets a tick
    refused compaction because the maintainer could not probe a declared base).

    Both facts come off the same handle deliberately: this read already existed for the version, and a
    second open to learn the bases would double the manifest reads on every vend.

    HERE RATHER THAN IN AN ENDPOINT MODULE, for the reason :func:`unsanctioned_bases` states: the vend
    door and describe-with-vending must answer from the same read, and a private copy in each is how
    their two answers came to differ.

    Base spellings are normalised through :func:`same_store_uri` because a manifest states a base in
    the manifest's own spelling, which may be schemeless — the policy needs a bucket and a key.
    """
    import lance  # lazy, matching this module's STS-client style: pylance loads only where vending runs

    from service_kit.lakehouse.features import manifest_base_path_refs
    from service_kit.lakehouse.objectfs import same_store_uri

    try:
        ds = lance.dataset(location, storage_options=storage_options, session=shared_lance_session())
    except (ValueError, OSError):
        return 0, ()
    bases: list[str] = []
    try:
        for ref in manifest_base_path_refs(ds):
            bases.append(same_store_uri(location, ref.path))
    except Exception:
        # A base we cannot SPELL is one the policy must not guess at. Vending without it yields exactly
        # today's behaviour — the narrower credential — rather than a wrong grant.
        log.warning("vend_base_paths_unreadable", extra={"location": location}, exc_info=True)
        bases = []
    return int(ds.version), tuple(bases)


def build_session_policy(
    bucket: str,
    prefix: str,
    tier: Tier,
    bases: Sequence[str] = (),
    *,
    sanctioned_bases: Sequence[str] = (),
    branch: str = "",
) -> dict[str, object]:
    """Build an STS inline session policy scoping access to one table prefix + tier.

    Two statements: ``s3:ListBucket`` on the bucket gated by an ``s3:prefix``
    condition, plus object actions on ``bucket/<prefix>/*``. Read tier =
    GET + List; write tier additionally allows PUT / DELETE /
    AbortMultipartUpload. As an STS *session* policy this can only RESTRICT the
    catalog's role (intersection-only), never widen it.

    ``bases`` are the base paths the table's manifest declares, granted READ and never write, at either
    tier, and only when :func:`_base_is_sanctioned` allows it — inside the table's own vended scope, or
    on the operator's ``sanctioned_bases`` allowlist. A table whose fragments carry a ``base_id``
    resolves them THROUGH those paths, so a credential that cannot read them is scoped to less than the
    table actually is — measured 2026-09-08 as 69 datasets a tick refused compaction because the
    maintainer could not probe a declared base (§ H12).

    ``sanctioned_bases`` defaults to EMPTY, which sanctions nothing foreign: a caller that has not wired
    the operator's allowlist grants only bases inside the table's own scope. Fail-closed is the right
    empty case here because the value being defaulted is a permission.

    READ ONLY is the whole of the privilege being added, and the asymmetry is deliberate: the table
    reads through the base, it does not own it, and a write grant on another dataset's root is exactly
    the blast radius vending exists to bound. § C1's remaining two rights — write on ``target_bases``,
    nothing on reference-only bases — need evidence a manifest read does not yet distinguish.

    One grant shape serves both base layouts, and the spec is why (``file_format.md`` § Base Path
    System): for ``is_dataset_root`` the files sit under the base's ``data/``/``_deletions/``/
    ``_indices/``, and for a plain base "the base path points directly to the file directory" — both
    UNDER the base path, so ``<base>/*`` covers each without the policy having to read the flag. It also
    covers ``_versions/``, which is what the maintainer's dataset-root probe actually asks for.

    Raises:
        ValueError: if ``prefix`` or any base carries an IAM wildcard metachar (``*``/``?``) — see
            :func:`_reject_iam_metacharacters` — or if a base names a bucket root rather than a location
            inside one, which would widen the credential to that whole bucket
            (:func:`_reject_a_base_that_is_not_a_location`).
    """
    _reject_iam_metacharacters("prefix", prefix)
    prefix = prefix.rstrip("/")
    # A BRANCH IS A PREFIX, and the format says which one ([[LH-055]]). `file_format.md` puts a branch's
    # files at `{dataset_root}/tree/{branch_name}/` and states the name is used "as is to form the path,
    # which means `/` would create a logical subdirectory (e.g. `bugfix/issue-123`)" — so the branch name
    # is joined verbatim and a `/` inside it is intended, not an attack.
    #
    # `..` IS the attack, and it is the one thing a session policy must never permit: the whole point of
    # scoping to `<prefix>/*` is that the credential cannot leave the table, and a climbing name would
    # rewrite the resource ARN to somewhere else in the bucket.
    branch = branch.strip("/")
    if branch:
        _reject_iam_metacharacters("branch", branch)
        if ".." in branch.split("/"):
            raise ValueError(f"branch {branch!r} may not traverse out of the table's prefix")
    # READ-ONLY ON MAIN, WRITE-ONLY ON THE BRANCH — `lancemultibasebranchingblobv2.md` § "Building
    # block 3" names this as the governance isolation the `tree/` layout exists to give. Main stays
    # READABLE because a branch manifest references its parent's fragments through a base pointing at
    # the dataset root; a credential that could not read them would be scoped to less than the branch is.
    #
    # A READ tier needs no split: it is already read-everywhere-in-scope, and a second statement
    # granting nothing new is one more thing to get wrong.
    branch_write = bool(branch) and tier == "write"
    obj_actions = list(_READ_ACTIONS if branch_write else (_WRITE_ACTIONS if tier == "write" else _READ_ACTIONS))
    list_prefixes = [f"{prefix}/*"] if prefix else ["*"]
    obj_resource = f"arn:aws:s3:::{bucket}/{prefix}/*" if prefix else f"arn:aws:s3:::{bucket}/*"
    statements: list[dict[str, object]] = [
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
    ]
    if branch_write:
        statements.append(
            {
                "Sid": "BranchObjects",
                "Effect": "Allow",
                "Action": list(_WRITE_ACTIONS),
                "Resource": f"arn:aws:s3:::{bucket}/{prefix}/tree/{branch}/*" if prefix else f"arn:aws:s3:::{bucket}/tree/{branch}/*",
            }
        )
    n = 0
    for base in bases:
        _reject_iam_metacharacters("base path", base)
        base_bucket, base_prefix = split_s3_location(base)
        base_prefix = base_prefix.rstrip("/")
        _reject_a_base_that_is_not_a_location(base, base_prefix)
        if not _base_is_sanctioned((base_bucket, base_prefix), (bucket, prefix), sanctioned_bases):
            # DROPPED, not raised, unlike the two guards above: a manifest is writer-chosen, and one
            # poisoned base must not make a table permanently un-vendable. The caller keeps the
            # credential it was entitled to and loses only the grant it was not.
            #
            # WARNING rather than silence because the failure this produces is remote from its cause: a
            # dropped base surfaces later as a read denial at the object store, on whoever used the
            # credential, with nothing naming the base.
            log.warning(
                "vend_base_path_unsanctioned",
                extra={"base": base, "table_bucket": bucket, "table_prefix": prefix, "sanctioned_count": len(sanctioned_bases)},
            )
            continue
        # A base may live in another BUCKET, so it needs its own pair of statements rather than another
        # resource on the table's: the ListBucket resource IS the bucket ARN.
        #
        # `n` counts SURVIVING bases, not declared ones: Sids are positional, and numbering by the
        # declared index would leave a `BaseObjects1` with no `BaseObjects0` whenever one is dropped.
        statements.append(
            {
                "Sid": f"ListBase{n}",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": f"arn:aws:s3:::{base_bucket}",
                "Condition": {"StringLike": {"s3:prefix": [f"{base_prefix}/*"] if base_prefix else ["*"]}},
            }
        )
        statements.append(
            {
                "Sid": f"BaseObjects{n}",
                "Effect": "Allow",
                "Action": list(_READ_ACTIONS),
                "Resource": f"arn:aws:s3:::{base_bucket}/{base_prefix}/*" if base_prefix else f"arn:aws:s3:::{base_bucket}/*",
            }
        )
        n += 1
    return {"Version": "2012-10-17", "Statement": statements}


class ModeBVendor:
    """No vending: data flows through the catalog's server-mediated endpoints."""

    def vend(
        self, *, table_location: str, tier: Tier, web_identity_token: str | None = None, bases: Sequence[str] = (), branch: str = ""
    ) -> VendedCredentials | None:
        return None


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
    OIDC token, so it cannot serve the cascade at all: a stage runner authenticates with ``dapr-api-token`` +
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
        access_key: str | None = None,
        secret_key: str | None = None,
        encryption: EncryptionAtRest | None = None,
        sanctioned_bases: Sequence[str] = (),
    ) -> None:
        #: Travels with every vend: a direct writer has no other channel to learn it (§ J5).
        self._encryption = encryption or EncryptionAtRest()
        #: The operator's allowlist of legitimate FOREIGN base paths. Deployment config rather than a
        #: `vend` argument: which buckets belong to this lakehouse is not a per-request question, and a
        #: per-request one would be answerable by the caller whose manifest is the untrusted input.
        self._sanctioned_bases = tuple(sanctioned_bases)
        self._role_arn = role_arn
        self._region = region
        self._endpoint = endpoint
        self._ttl = ttl_seconds
        #: The credential this vendor DELEGATES FROM. Required, not optional in practice: AssumeRole is
        #: a signed call and botocore's default chain finds nothing here, because the S3 secret reaches
        #: `Settings` from the Dapr secret store and never the process env.
        self._access_key = access_key
        self._secret_key = secret_key
        self._assume_role = assume_role or self._default_assume_role
        self._client: Any = None  # boto3 STS client, built once (the vendor is a lifespan singleton)

    def _default_assume_role(self, **kwargs: object) -> dict[str, object]:
        if self._client is None:
            from storage import sts_client  # lazy: only needed when STS vending is actually enabled

            # `storage` is where boto3 is declared and imported, so the timeouts and retry policy are
            # decided once; a client built here would inherit botocore's timeout-free defaults and an
            # unresponsive STS endpoint would hang the vend instead of failing it.
            self._client = sts_client(region=self._region, endpoint=self._endpoint, access_key=self._access_key, secret_key=self._secret_key)
        return cast(dict[str, object], self._client.assume_role(**kwargs))

    def vend(
        self, *, table_location: str, tier: Tier, web_identity_token: str | None = None, bases: Sequence[str] = (), branch: str = ""
    ) -> VendedCredentials | None:
        bucket, prefix = split_s3_location(table_location)
        policy = build_session_policy(bucket, prefix, tier, bases, sanctioned_bases=self._sanctioned_bases, branch=branch)
        resp = self._assume_role(
            RoleArn=self._role_arn,
            RoleSessionName="lance-catalog-vend",
            Policy=json.dumps(policy),
            DurationSeconds=self._ttl,
        )
        creds = cast(dict[str, object], resp["Credentials"])
        # BUILT THROUGH THE SHARED BUILDER, not hand-rolled. The hand-rolled dict carried the key pair,
        # the token, the region and the endpoint — and Lance refused to construct a client from it,
        # failing in 13µs with `HTTP error: builder error` (measured 2026-09-03). object_store will not
        # build an S3 client for an `http://` endpoint without `allow_http`, and RustFS/MinIO reject
        # virtual-hosted signing with 403 `SignatureDoesNotMatch`. A credential that is correct and
        # unusable is the worst shape: the vend returns 200 with a full triple and every use of it fails
        # somewhere else. `lance_storage_options`' own docstring names this — "one omitted key in a
        # hand-rolled copy is exactly the drift this builder exists to prevent".
        opts = lance_storage_options(
            self._endpoint or "",
            str(creds["AccessKeyId"]),
            str(creds["SecretAccessKey"]),
            self._region,
            session_token=str(creds["SessionToken"]),
            **self._encryption.as_options(),  # ty: ignore[invalid-argument-type]
        )
        if not self._endpoint:
            # AWS proper: botocore resolves the regional endpoint, and an empty string would pin the
            # client to nothing. The builder always emits the key, so it is removed rather than skipped.
            opts.pop("endpoint", None)
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
        encryption: EncryptionAtRest | None = None,
        sanctioned_bases: Sequence[str] = (),
    ) -> None:
        #: Travels with every vend: a direct writer has no other channel to learn it (§ J5).
        self._encryption = encryption or EncryptionAtRest()
        #: The operator's allowlist of legitimate FOREIGN base paths — see :class:`StsVendor`. Both
        #: plugs carry it because both render a session policy from a writer-chosen manifest.
        self._sanctioned_bases = tuple(sanctioned_bases)
        self._region = region
        self._endpoint = endpoint
        self._role_arn = role_arn  # RustFS ignores it; boto3 requires the param
        self._ttl = ttl_seconds
        self._assume = assume or self._default_assume
        self._client: Any = None  # boto3 STS client, built once (the vendor is a lifespan singleton)

    def _default_assume(self, **kwargs: object) -> dict[str, object]:
        if self._client is None:
            from storage import sts_client  # lazy: only when web_identity vending is enabled

            # `unsigned`: token-authenticated, not SigV4 — the JWT in the body is the credential, and
            # this caller holds no key to sign with. Built through `storage` so the connect/read
            # timeouts apply here too; the vend is on a data-plane request's critical path.
            self._client = sts_client(region=self._region, endpoint=self._endpoint, unsigned=True)
        return cast(dict[str, object], self._client.assume_role_with_web_identity(**kwargs))

    def vend(
        self, *, table_location: str, tier: Tier, web_identity_token: str | None = None, bases: Sequence[str] = (), branch: str = ""
    ) -> VendedCredentials | None:
        if not web_identity_token:  # no caller token to exchange → fall back to server-mediated
            return None
        bucket, prefix = split_s3_location(table_location)
        resp = self._assume(
            RoleArn=self._role_arn,
            RoleSessionName="lance-catalog-vend",
            WebIdentityToken=web_identity_token,
            Policy=json.dumps(build_session_policy(bucket, prefix, tier, bases, sanctioned_bases=self._sanctioned_bases, branch=branch)),
            DurationSeconds=self._ttl,
        )
        creds = cast(dict[str, object], resp["Credentials"])
        # Through the ONE builder, like `StsVendor` beside it. Hand-rolling this dict is how it came to
        # carry the bare credential spellings, which do not displace a pod's ambient AWS_* environment —
        # object_store blends the two and signs as neither identity (`403 SignatureDoesNotMatch`,
        # measured in-cluster 2026-09-03). It also silently omitted `allow_http` and
        # `virtual_hosted_style_request`, which RustFS needs.
        opts = lance_storage_options(
            self._endpoint or "",
            str(creds["AccessKeyId"]),
            str(creds["SecretAccessKey"]),
            self._region,
            session_token=str(creds["SessionToken"]),
            **self._encryption.as_options(),  # ty: ignore[invalid-argument-type]
        )
        if not self._endpoint:
            opts.pop("endpoint", None)
        return VendedCredentials(storage_options=dict(opts), expires_at_millis=_expiry_millis(creds.get("Expiration"), self._ttl))


def make_vendor(
    mode: VendingMode,
    *,
    region: str = "us-east-1",
    sts_endpoint: str | None = None,
    assume_role_arn: str | None = None,
    ttl_seconds: int = 900,
    access_key: str | None = None,
    secret_key: str | None = None,
    encryption: EncryptionAtRest | None = None,
    sanctioned_bases: Sequence[str] = (),
) -> CredentialVendor:
    """Build the configured :class:`CredentialVendor`.

    Raises:
        ValueError: for ``sts`` mode without ``assume_role_arn``, or an unknown mode.
    """
    if mode == "mode_b":
        return ModeBVendor()
    if mode == "sts":
        if not assume_role_arn:
            raise ValueError("assume_role_arn is required for sts vending mode")
        return StsVendor(
            role_arn=assume_role_arn,
            region=region,
            endpoint=sts_endpoint,
            ttl_seconds=ttl_seconds,
            access_key=access_key,
            secret_key=secret_key,
            encryption=encryption,
            sanctioned_bases=sanctioned_bases,
        )
    if mode == "web_identity":
        return WebIdentityVendor(
            region=region,
            endpoint=sts_endpoint,
            role_arn=assume_role_arn or _DEFAULT_VEND_ROLE_ARN,
            ttl_seconds=ttl_seconds,
            encryption=encryption,
            sanctioned_bases=sanctioned_bases,
        )
    assert_never(mode)
