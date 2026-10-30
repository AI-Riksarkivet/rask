"""Credential vending for the catalog data plane.

The catalog authenticates (OIDC) and authorizes (OpenFGA), then a :class:`CredentialVendor` turns *(table
object-store location, access tier)* into the ``storage_options`` a client (LanceDB SDK / lance-ray / pylance) uses
to reach object storage directly. The target is **S3-compatible** storage: the platform's object store in production
(AWS S3, Ceph RGW, RustFS), and RustFS as the development store. Two plugs:

* :class:`WebIdentityVendor` — STS ``AssumeRoleWithWebIdentity`` with the CATALOG'S OWN projected ServiceAccount
  token and an inline session policy: a short-TTL credential narrowed to one table and tier, under the policy the
  store grants the catalog's subject ([[XC-083]], ADR 0170). The vend's parent is the catalog's ServiceAccount, so
  the catalog holds no store key to sign it with, and the caller's own token never leaves the catalog.
* :class:`ModeBVendor` — ``vend`` returns ``None``: no credential ever leaves the catalog; the client uses the
  server-mediated (Arrow-IPC) data endpoints. The backend-agnostic default — nothing is delegated.

WHAT THE DEVELOPMENT STORE ANSWERS IS MEASURED, not inferred, because getting it backwards costs the whole data
plane: a vendor the store refuses vends nothing, and every direct-to-store read falls back or fails. Measured
2026-10-18 on ``rustfs/rustfs`` 1.0.3 (`docs/audits/2026-10-18/rustfs_web_identity_measure.py`): a Kubernetes-shaped
token for the store's audience whose subject names a canned policy is exchanged; its session policy is enforced as an
intersection with that policy (a read session's GET in scope 200, another prefix, another tenant and a PUT 403, and a
session policy naming a bucket the canned policy lacks grants nothing); Lance creates and appends a dataset on the
session; and another ServiceAccount's token, another audience, an expired token, a forged one and another issuer's
are each refused.

OpenFGA decides the tier: ``can_read_data`` -> ``"read"``, ``can_write_data`` -> ``"write"``,
``can_vend_maintain`` -> ``"maintain"`` (:func:`build_session_policy` says what each grants).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from typing import Any, Final, Literal, Protocol, assert_never, runtime_checkable
from urllib.parse import urlsplit

from botocore.exceptions import BotoCoreError, ClientError
from lance_namespace import InternalError, InvalidInputError, ServiceUnavailableError
from pydantic import BaseModel, ConfigDict, Field

from catalog.core.base_judge import BaseJudge, require_sanctioned_bases
from catalog.core.namespace import open_location
from service_kit.governed import store_session
from service_kit.governed.machine_identity import IdentityTokenUnavailableError
from service_kit.governed.store_credential import S3KeyPair
from service_kit.governed.store_session import IssuedSession, WebIdentity
from service_kit.lakehouse.base_refs import decoded_path, location_within
from service_kit.lakehouse.base_registry import BaseJudgement, BaseRole, BaseStanding
from service_kit.lakehouse.branch_layout import BRANCH_CONTAINER, BRANCH_FILE_DIRS
from service_kit.lakehouse.features import BasePathRef
from service_kit.lakehouse.objectfs import lance_storage_options
from service_kit.lakehouse.table_layout import BRANCH_STAGING_GLOB, FINAL_MANIFEST_GLOB, WRITER_PUT_GLOBS


log = logging.getLogger(__name__)

#: What a credential may do inside the prefix it is scoped to ([[LH-202]]):
#:
#: * ``read`` — list and get the table.
#: * ``write`` — a ``can_write_data`` managed-versioning writer: read the table, put what a commit needs
#:   (data, transaction, deletion and index files, and a staged manifest), never a final manifest, a ref or
#:   a delete. The catalog commits the version.
#: * ``maintain`` — the maintenance identity's rewrite and reclaim (``can_vend_maintain``): get, put and delete anywhere in the prefix,
#:   because compaction commits versions and cleanup deletes them. Also the shape for a prefix with no
#:   Lance layout to narrow by (the warehouse scope probe).
Tier = Literal["read", "write", "maintain"]
VendingMode = Literal["mode_b", "web_identity"]


class EncryptionAtRest(BaseModel):
    """What a vended credential must tell the writer about encrypting the bytes it lands (§ J5).

    THE POINT IS THE DIRECT WRITE. A client vended credentials writes to object storage without the
    catalog in the path, so any encryption the warehouse intends has to travel IN the storage options
    or it does not happen. Key names and their contract are verified against `lance_docs/lance/guide/object_store.md`
    (134-136), not guessed — `lance_storage_options` refuses a combination the store would ignore.

    Empty is the default and means "whatever the bucket does", which is what every deployment had
    before this existed: unset here changes nothing.
    """

    algorithm: str | None = None
    kms_key_id: str | None = None
    bucket_key_enabled: bool | None = None


class VendedSession(BaseModel):
    """A session vended for one table at one tier ([[LH-072]]): the key pair STS issued and how a client reaches the store with it.

    The secret access key and the session token are ``SecretStr`` (:class:`S3KeyPair`), so a repr, a str, a model dump, a
    log line or an error that echoes a vend carries neither; Lakekeeper derives ``Redact`` on its credential structs for
    the same reason. They leave the catalog only through :meth:`as_storage_options`, called where the vend is handed
    over or used.
    """

    model_config = ConfigDict(frozen=True)

    key_pair: S3KeyPair
    #: The object-store address the session is used at; ``None`` is AWS proper, whose regional endpoint botocore resolves.
    endpoint: str | None
    region: str
    #: Travels with every vend: a direct writer has no other channel to learn it (§ J5).
    encryption: EncryptionAtRest = Field(default_factory=EncryptionAtRest)
    #: When the client must refresh: every vend is a session STS issued, and every such session expires.
    expires_at_millis: int

    def as_storage_options(self) -> dict[str, str]:
        """The flat ``storage_options`` a client signs with, the secret and the session token in clear.

        The spec's one map (`lance_docs/ns_catalog/spec.yaml:2883-2892`), at the keys
        `lance_docs/lance/guide/object_store.md:127-136` names. Called only where the vend leaves the catalog: the body
        that hands it to its caller, or the client the catalog signs with it.

        BUILT THROUGH THE SHARED BUILDER, not hand-rolled. A hand-rolled dict carried the bare credential spellings, which
        do not displace a pod's ambient AWS_* environment — object_store blends the two and signs as neither identity
        (`403 SignatureDoesNotMatch`, measured in-cluster 2026-09-03) — and omitted `allow_http` and
        `virtual_hosted_style_request`, without which Lance builds no client for an `http://` endpoint and RustFS refuses
        virtual-hosted signing.

        Raises:
            ValueError: The encryption names a combination the store would ignore (`lance_storage_options`).
        """
        session_token = self.key_pair.session_token
        options = lance_storage_options(
            self.endpoint or "",
            self.key_pair.access_key_id,
            self.key_pair.secret_access_key.get_secret_value(),
            self.region,
            session_token=session_token.get_secret_value() if session_token is not None else None,
            server_side_encryption=self.encryption.algorithm,
            sse_kms_key_id=self.encryption.kms_key_id,
            sse_bucket_key_enabled=self.encryption.bucket_key_enabled,
        )
        if not self.endpoint:
            # AWS proper: botocore resolves the regional endpoint, and an empty string would pin the client to
            # nothing. The builder always emits the key, so it is removed rather than skipped.
            options.pop("endpoint", None)
        return options


@runtime_checkable
class CredentialVendor(Protocol):
    """Vend scoped storage credentials for one table prefix at one tier."""

    def vend(
        self, *, table_location: str, tier: Tier, session_name: str, bases: Sequence[str] = (), data_directories: Sequence[str] = (), branch: str = ""
    ) -> VendedSession | None:
        """Return creds for ``table_location`` at ``tier``, named ``session_name`` at the store (:func:`role_session_name`).

        ``data_directories`` are those of ``bases`` the table's record holds as its own data directories
        (:class:`VendableBases`). ``None`` means "no direct credential" — the caller falls back to the server-mediated
        (Mode B) data path.
        """
        ...


def split_s3_location(location: str) -> tuple[str, str]:
    """Return ``(bucket, key_prefix)`` for an ``s3://bucket/key...`` location, the prefix as the store keeps it.

    ``describe_table`` answers a registered location holding a non-ASCII character or a ``%`` percent-encoded
    (``…/r%C3%A4ksm%C3%B6rg%C3%A5s``), and pylance opens the key it decodes to once (``base_refs.decoded_path``), so a
    policy naming the encoded prefix reaches none of the table's bytes: measured as 403 AccessDenied on helm rev 20
    ([[LH-517]], [[LH-520]]).

    Raises:
        ValueError: if ``location`` has no bucket (authority) component.
    """
    if not urlsplit(location).netloc:
        raise ValueError(f"location has no bucket: {location!r}")
    bucket, _, prefix = decoded_path(location).partition("/")
    return bucket, prefix


_READ_ACTIONS = ("s3:GetObject",)
#: A writer lands new objects and may abandon its own multipart upload; it deletes nothing.
_APPEND_ACTIONS = ("s3:PutObject", "s3:AbortMultipartUpload")
_MAINTAIN_ACTIONS = (
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:AbortMultipartUpload",
)
#: What a maintainer does in its table's own data directory: read it, and delete what no version references ([[LH-286]]).
#: No put: a compaction writes its rewrite to the table's root, since pylance 13.0.0's ``CompactionOptions`` names no base.
_DATA_DIRECTORY_MAINTAIN_ACTIONS = ("s3:GetObject", "s3:DeleteObject")
#: What S3 asks of the KMS key an object is encrypted with: ``kms:Decrypt`` to GET one, ``kms:GenerateDataKey`` to PUT one,
#: and both for a multipart upload (AWS, "Using server-side encryption with AWS KMS keys") ([[LH-262]]).
_KMS_READ_ACTIONS = ("kms:Decrypt",)
_KMS_WRITE_ACTIONS = ("kms:Decrypt", "kms:GenerateDataKey")

#: The longest inline session policy STS takes: 2,048 characters of the plaintext on AWS, and 2,048 bytes as RustFS
#: re-serialises it (`rustfs@1.0.3:rustfs/src/admin/service/session_policy.rs:34-39`). The policy is sent as compact,
#: escaped (so ASCII) JSON, which neither store re-serialises any shorter, so its length is the bound ([[LH-262]]).
SESSION_POLICY_LIMIT: Final = 2048


class SessionPolicyTooLong(InternalError):
    """A vend whose session policy STS would refuse: its table's location, bases or KMS key are too long ([[LH-262]]).

    A 5xx's ``detail`` is redacted (``ns_errors.problem_detail``), so the cause travels in the problem's title (this class's
    name) and in extension members the catalog wrote itself: the policy's length and the limit.
    """

    #: RFC 9457 extension members, merged into the problem body by ``ns_errors.problem_detail``.
    problem_extra: dict[str, object]

    def __init__(self, message: str, *, characters: int) -> None:
        super().__init__(message)
        self.problem_extra = {"session_policy_characters": characters, "session_policy_limit": SESSION_POLICY_LIMIT}


#: What AWS lets a RoleSessionName hold (2 to 64 of `[\w+=,.@-]`); RustFS accepts the name and keeps none of it.
_SESSION_NAME_UNSAFE: Final = re.compile(r"[^A-Za-z0-9_+=,.@-]")
_SESSION_NAME_MAX: Final = 64


def role_session_name(subject: str) -> str:
    """The STS session name a vend for ``subject`` carries: the subject, each character AWS refuses turned to ``-``.

    The name is what AWS's CloudTrail records a vended session under, so a session reads as the person or service
    it was vended for (`kubernetes~system:serviceaccount:rask:rask-sa-maintenance` becomes
    `kubernetes-system-serviceaccount-rask-rask-sa-maintenance`). Longer than 64 characters, it is cut; shorter than
    the two AWS requires, it is padded with ``-``.
    """
    return _SESSION_NAME_UNSAFE.sub("-", subject)[:_SESSION_NAME_MAX].ljust(2, "-")


#: What IAM reads as syntax inside a Resource ARN and an ``s3:prefix`` condition, not as a character of the value. ``*``
#: and ``?`` are wildcards there, so a value holding one widens the grant to siblings: a ``foo*`` prefix authorises
#: ``bucket/foo*/*``, which matches ``foobar``. ``${`` opens a policy variable, which IAM substitutes as it evaluates the
#: policy, so a value holding one names whatever the variable holds rather than itself ([[LH-526]]). ONE tuple for every
#: door that lets a value into a vended policy: the register door refuses a location holding any of them
#: (``table_bases.require_registrable_location``), the policy builder refuses a table prefix holding one and drops a
#: declared base holding one, since the table's writer chose it ([[LH-437]]), and a branch holding one is refused. A
#: second copy is a second thing that can fall out of step with what IAM reads as syntax.
IAM_POLICY_SYNTAX: Final = ("*", "?", "${")


def held_iam_policy_syntax(value: str) -> list[str]:
    """The :data:`IAM_POLICY_SYNTAX` ``value`` holds, which no statement can name without IAM reading it as syntax."""
    return [token for token in IAM_POLICY_SYNTAX if token in value]


def _reject_iam_policy_syntax(what: str, value: str) -> None:
    """Refuse a value holding IAM policy syntax (:data:`IAM_POLICY_SYNTAX`), which a vended policy cannot name.

    The table prefix goes through here, and raises: it is a uuid the catalog minted for a table it made, or a location
    the register door admitted (``table_bases.require_registrable_location``), which refuses the syntax, so a prefix
    holding it is the catalog's own fault. A base path comes off the table's MANIFEST, which its writer chose, so the
    base loop of :func:`build_session_policy` drops one holding it instead ([[LH-437]]).
    """
    if held := held_iam_policy_syntax(value):
        raise ValueError(f"{what} {value!r} holds IAM policy syntax {held}; it would widen or redirect the vended policy")


def _reject_a_base_that_is_not_a_location(base: str, base_prefix: str) -> None:
    """A base must name a place INSIDE a bucket, never the bucket itself.

    With no prefix the base's object statement collapses to ``arn:aws:s3:::<bucket>/*`` — measured
    2026-09-11, a base of ``s3://lakehouse`` grants READ on the whole lakehouse bucket and a base of
    ``s3://rask-observability`` grants READ on a bucket the table has nothing to do with. One declared
    base would turn a credential scoped to a single table prefix into a bucket-wide reader.

    THE INPUT IS CHOSEN BY A WRITER, which is why depth has to be checked rather than assumed. Bases are
    read from the table's own manifest, and a maintain-tier vend grants ``PutObject`` on ``<prefix>/*``,
    which covers ``_versions/`` — enough to commit a manifest client-side; the static keys the cascade's
    writers hold reach it too. So the value here is one a principal on ONE table can pick, used to widen
    the next credential vended for it.
    The base loop already drops a base holding IAM policy syntax (:func:`held_iam_policy_syntax`); this is the
    other half of the same distrust.

    REFUSING A BUCKET ROOT CANNOT NARROW A REAL TABLE: the spec's base path points at a dataset root or
    a file directory (``lance_docs/lance/format/table/layout.md``, Base Path System), never at a bucket. A base in its OWN bucket
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
    the vend door did not, which is the asymmetry this closes. An allowlist entry sanctions a directory
    BENEATH it and never itself (:func:`_location_strictly_within`): each table registers its own
    directory there, and the entry's whole prefix is every table's.

    The alternative considered and rejected was resolving each base to a catalog table and checking the
    caller's read rung on it. There is no location->table index, so that costs either a walk of the
    estate on a 900 s-TTL hot path or a reversal of the backend's own layout convention. The operator's
    allowlist answers the same question — is this foreign location a legitimate part of this lakehouse —
    without either.
    """
    if _location_within(table, base):
        return True
    return any(_location_strictly_within(split_s3_location(entry), base) for entry in sanctioned_bases)


def _location_strictly_within(outer: tuple[str, str], inner: tuple[str, str]) -> bool:
    """True iff ``inner`` lies BENEATH ``outer`` — the approved base itself is not a table's directory ([[LH-252]]).

    Every table on an approved data base registers its own directory under it
    (``dataplane.table_data_bases``), so a grant at the approved base is a grant on every sibling's
    fragments. A manifest declaring the approved base itself is therefore granted nothing: the vend
    reaches one table's directory or no part of the base.
    """
    return _location_within(outer, inner) and inner[1].rstrip("/") != outer[1].rstrip("/")


def unsanctioned_bases(table_location: str, bases: Sequence[str], sanctioned_bases: Sequence[str] = ()) -> tuple[str, ...]:
    """The declared bases a vended session policy would NOT be able to grant.

    [[LH-057]]. The vend door refuses a direct credential to any multi-base table, which was right
    while the policy was scoped to the primary bucket alone: a client would be denied at the object
    store on the first fragment that lived elsewhere. The policy grants every sanctioned base now
    (:func:`build_session_policy`), so the question is no longer "does this table have bases" but
    "is there a base this credential could not reach" — and only the second justifies proxying the
    table through the catalog's own credential.

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

    A base inside the table's own location needs no grant of its own, whatever its spelling: the table
    statement already covers it. That is every base a BRANCH handle declares — its parent's root — and
    the vend doors judge the branch's own manifest ([[LH-279]]). A base outside it holding IAM policy syntax
    is one no statement can name without IAM reading the syntax, so the policy drops it, and the door answers server-mediated
    rather than vend a credential that misses it ([[LH-437]]).
    """
    if location_within(table_location, base):
        return True
    if held_iam_policy_syntax(base):
        return False
    try:
        return _base_is_sanctioned(split_s3_location(base), split_s3_location(table_location), sanctioned_bases)
    except ValueError:
        return False


#: The field-metadata key a classification is written under.
#:
#: LANCE FIELD METADATA IS THE CLASSIFICATION STORE, and no new one is needed ([[LH-058]]). Measured on
#: pylance 2026-09-22: `update_field_metadata({"secret": {...}})` round-trips through a reopen, and both
#: the metadata AND the field's id (`LanceField.id()`) survive a RENAME — `secret` -> `ssn` kept id 1 and
#: kept the value. That stability across a rename is precisely what the reference model keys its column
#: tags on FIELD-ID to obtain, and here it is a property of the format rather than something to build.
#:
#: NAMESPACED so it cannot collide with a user's own field metadata: this key decides whether bytes are
#: vendable, and a table author who happens to use the word `classification` must not be able to change
#: that by accident.
CLASSIFICATION_KEY: Final = "rask.classification"


def labelled_fields(dataset: Any) -> dict[int, tuple[str, dict[str, str]]]:  # noqa: ANN401 — a lance.LanceDataset
    """Every field carrying ``rask.*`` metadata, nested ones included, keyed by its Lance field id: its dotted path and
    those keys.

    KEYED BY FIELD ID, the identity Lance gives a field: a rename keeps it and its metadata, while a re-type re-mints the
    id with no metadata (measured on pylance 13.0.0), which is what a door's commit is held to
    (``dataplane.commit_stamped``'s ``keeps_labels``).

    Raises:
        OSError: The dataset's schema could not be read.
    """
    found: dict[int, tuple[str, dict[str, str]]] = {}

    def walk(field: Any, prefix: str) -> None:  # noqa: ANN401 — a lance.LanceField
        path = f"{prefix}{field.name()}"
        governed = {str(key): str(value) for key, value in (field.metadata or {}).items() if str(key).startswith("rask.")}
        if governed:
            found[int(field.id())] = (path, governed)
        for child in field.children():
            walk(child, f"{path}.")

    for field in dataset.lance_schema.fields():
        walk(field, "")
    return found


def classified_columns(dataset: Any) -> tuple[str, ...]:  # noqa: ANN401 — a lance.LanceDataset
    """The dotted paths of this ref's fields carrying :data:`CLASSIFICATION_KEY`, nested ones included, sorted.

    NAMES for the human-readable refusal; the CLASSIFICATION itself lives on the field and travels with it through a
    rename.

    Raises:
        OSError: The ref's schema could not be read, which no caller may read as "nothing is classified".
    """
    return tuple(sorted(path for path, governed in labelled_fields(dataset).values() if CLASSIFICATION_KEY in governed))


def classified_in_any_ref(dataset: Any, *, branch: str = "") -> tuple[str, ...]:  # noqa: ANN401 — a lance.LanceDataset
    """The classified paths of every ref of the table ``dataset`` is a ref of (``branch``, ``""`` for main), each named
    ``<ref>:<path>`` for a branch, sorted.

    A credential for any ref reads every ref's files: a stock client opens a branch with the table's own credential
    (``checkout_version((branch, None))``), and a branch reads its parent's fragments through a base on the table root
    (`lance_docs/lance/format/table/layout.md` § Shallow Clone), so a label on one ref holds every vend back
    ([[LH-207]]). A ref whose schema cannot be read is named as unreadable rather than left out, and refs that cannot be
    listed hold the vend back too.
    """
    try:
        main = dataset if not branch else dataset.checkout_version(("main", None))
        found = set(classified_columns(main))
        branches = list(dataset.branches.list())
    except (AttributeError, ValueError, OSError):
        log.warning("vend_refs_unreadable", exc_info=True)
        return ("<refs unreadable>",)
    for name in branches:
        try:
            found.update(f"{name}:{path}" for path in classified_columns(dataset.checkout_version((name, None))))
        except (AttributeError, ValueError, OSError):
            log.warning("vend_branch_schema_unreadable", extra={"branch": name}, exc_info=True)
            found.add(f"{name}:<unreadable>")
    return tuple(sorted(found))


class VendFacts(BaseModel):
    """What a vend needs to know about the ref it vends, read from ONE open under the catalog's own credential."""

    #: The client's optimistic-append base; 0 for a declared-only table with no readable dataset yet.
    read_version: int = 0
    #: Every base the ref's manifest declares, as the manifest spells it — what the base judge reads.
    bases: list[BasePathRef] = Field(default_factory=list)
    #: The same bases in the table location's own spelling — what the session policy grants.
    base_uris: tuple[str, ...] = ()
    #: Fields carrying :data:`CLASSIFICATION_KEY` on any ref of the table ([[LH-058]], [[LH-207]]).
    classified: tuple[str, ...] = ()


def dataset_facts(location: str, storage_options: dict[str, str], *, branch: str = "") -> VendFacts:
    """The version and declared bases of the REF being vended, and the classified columns of every ref of its table, read
    under the catalog's own credential.

    The version is the client's optimistic-append base; 0 for a declared-only/new table with no
    readable dataset yet. The bases are what the vended policy must also be able to READ — a table
    whose fragments carry a ``base_id`` resolves them through those paths, so a credential scoped to
    the table prefix alone is scoped to less than the table is (§ H12, measured: 69 datasets a tick
    refused compaction because the maintainer could not probe a declared base).

    The version and bases come off the one handle :func:`catalog.core.namespace.open_location` opens, the open every
    other read of the table makes. The classified columns cost one more manifest read per branch, and one for main when
    the ref is a branch (:func:`classified_in_any_ref`): a label on any ref holds the credential back, since it reads
    every ref's files.

    ``branch`` IS THE REF THE CREDENTIAL IS FOR ([[LH-279]]), so it is the ref these facts describe: a
    base planted on ``tree/<b>`` is invisible from main (lh279 m7), and a branch credential read through
    main's manifest would be judged on a base list it does not use.

    HERE RATHER THAN IN AN ENDPOINT MODULE, for the reason :func:`unsanctioned_bases` states: the vend
    door and describe-with-vending must answer from the same read, and a private copy in each is how
    their two answers came to differ.

    Base spellings are normalised through :func:`same_store_uri` because a manifest states a base in
    the manifest's own spelling, which may be schemeless — the policy needs a bucket and a key.

    Raises:
        ServiceUnavailableError: The dataset opened and its base list could not be read. Answering "no
            bases" here would vend without judging them, which the base judge exists to refuse.
    """
    from service_kit.lakehouse.features import manifest_base_path_refs
    from service_kit.lakehouse.objectfs import same_store_uri

    try:
        ds = open_location(location, storage_options, branch=branch or None)
    except (ValueError, OSError):
        return VendFacts()
    try:
        refs = manifest_base_path_refs(ds)
        uris = tuple(same_store_uri(location, ref.path) for ref in refs)
    except Exception as exc:
        log.warning("vend_base_paths_unreadable", extra={"location": location, "branch": branch}, exc_info=True)
        raise ServiceUnavailableError("the table's declared bases could not be read, so no credential was vended for it") from exc
    return VendFacts(
        read_version=int(ds.version),
        bases=refs,
        base_uris=uris,
        classified=classified_in_any_ref(ds, branch=branch),
    )


class VendableBases(BaseModel):
    """The declared bases a vend may grant, and which of them the table's record holds as its own data directories."""

    model_config = ConfigDict(frozen=True)

    #: In the spelling and order of ``VendFacts.base_uris``.
    bases: tuple[str, ...] = ()
    #: Those of ``bases`` the record holds as ``data`` entries: the table's own directory beneath an approved base, which
    #: its claim keeps every other table out of ([[LH-252]]), so a maintainer may delete in it ([[LH-286]]).
    data_directories: tuple[str, ...] = ()


def require_vendable_bases(location: str, facts: VendFacts, judge: BaseJudge) -> VendableBases:
    """The declared bases a vend may grant, refusing a ref declaring a base the catalog did not sanction — both vending doors ask this ([[LH-279]]).

    A vended session policy grants READ on every declared base it can address, so vending for a table
    that planted another table's root hands its holder that table's bytes for the credential's lifetime.
    Judged by the same predicate as every read (:func:`catalog.core.base_judge.require_sanctioned_bases`):
    the table's own root, a configured external blob base, or its record. The operator's allowlists stay
    the policy's own second check (:func:`build_session_policy`), and gate what a create or register may
    record.

    AN EXTERNAL BLOB BASE IS SANCTIONED AND NEVER GRANTED ([[LH-209]]). It is a pointer base — every data
    file stays under the table's own root, and only blob descriptors name objects through it — and it is
    authorized per table and per object at the create door, while a credential's unit is a prefix: the
    default base is the model-artifact tree of every project, so granting it handed every vend every
    tenant's weights. Its bytes are served by the catalog's blob door instead. So the answer drops each
    base judged CONFIGURED, or RECORDED as ``external_blob``, from what the policy is asked to grant.

    Returns:
        The vendable bases, in the spelling and order of ``facts.base_uris``, with the table's own data directories among
        them named.

    Raises:
        InvalidTableStateError: A declared base is sanctioned by none of those (code 19, 409).
        ServiceUnavailableError: The table's record could not be read.
    """
    judged = list(zip(facts.base_uris, require_sanctioned_bases(location, facts.bases, judge=judge), strict=True))
    return VendableBases(
        bases=tuple(uri for uri, judgement in judged if not _is_pointer_base(judgement)),
        data_directories=tuple(uri for uri, judgement in judged if _is_own_data_directory(judgement)),
    )


def _is_own_data_directory(judgement: BaseJudgement) -> bool:
    return (
        judgement.standing is BaseStanding.RECORDED
        and judgement.entry is not None
        and judgement.entry.role is BaseRole.DATA
        and not judgement.entry.is_dataset_root
    )


def _is_pointer_base(judgement: BaseJudgement) -> bool:
    if judgement.standing is BaseStanding.CONFIGURED:
        return True
    return judgement.entry is not None and judgement.entry.role is BaseRole.EXTERNAL_BLOB


def table_has_branch(location: str, storage_options: dict[str, str], branch: str) -> bool:
    """Does this table have that branch? ([[LH-056]])

    A SEPARATE OPEN, and deliberately not a fourth fact on :func:`dataset_facts`. That function's own
    rule is that a fact needed on EVERY vend rides the one handle; a branch check is needed only when
    a caller names a branch, which is the rare path, so folding it in would add a read to every vend
    that names none.

    A TABLE WITH NO READABLE DATASET HAS NO BRANCHES, which is the right answer rather than a gap: a
    declared-only table has no versions, and a branch is taken from a version. So the unreadable case
    refuses rather than waving the name through — the same fail-closed direction the classification
    and base checks take.
    """
    try:
        return branch in open_location(location, storage_options).branches.list()
    except Exception:
        # ONE ARM, CATCHING ANYTHING, because every way of failing has the same right answer. A branch
        # set this cannot determine must not read as "this branch exists": refusing costs a caller one
        # retry, admitting hands out a 900 s grant on an unverified prefix. Narrower than
        # :func:`dataset_facts`'s `(ValueError, OSError)` on purpose — that one degrades to a narrower
        # credential, and this one decides whether a credential is minted at all.
        log.warning("vend_branch_unreadable", extra={"location": location, "branch": branch}, exc_info=True)
        return False


def build_session_policy(
    bucket: str,
    prefix: str,
    tier: Tier,
    bases: Sequence[str] = (),
    *,
    sanctioned_bases: Sequence[str] = (),
    data_directories: Sequence[str] = (),
    branch: str = "",
    kms_key: str | None = None,
) -> dict[str, object]:
    """Build an STS inline session policy scoping access to one table prefix + tier.

    Every tier lists and reads ``<prefix>/*`` (``s3:ListBucket`` gated by an ``s3:prefix`` condition,
    ``s3:GetObject`` on the objects). What else it grants is an allow-list from the layout
    `lance_docs/lance/format/table/layout.md` fixes, one rung per FGA relation ([[LH-202]]); its ``_blobs/`` and ``_bt/`` are
    written only under feature flags ``version/create`` refuses a commit turning on, so no tier puts there:

    * ``read``: nothing else.
    * ``write`` (``can_write_data``): put and abort-upload on what a managed-versioning writer stages under
      the ref it writes (``service_kit.lakehouse.table_layout.WRITER_PUT_GLOBS``: ``data/``,
      ``_transactions/``, ``_deletions/``, ``_indices/`` and a staged ``_versions/<n>.manifest-<uuid>``), and
      an explicit Deny on every final manifest under it, at any depth (``FINAL_MANIFEST_GLOB``). The table's
      own credential (no ``branch``) also puts under ``tree/`` ([[LH-490]]): a stock client opens a branch
      with the table's ``DescribeTable`` and stages the branch's commit with it (measured on pylance 13.0.0),
      and ``can_write_data`` on the table writes every branch. Nothing under ``_refs/`` or ``_mem_wal/``,
      and no delete. The catalog commits the version under its own credential, so a writer that could put a
      final manifest could commit Overwrite, Restore, UpdateBases or a DataReplacement around it, and one
      that could put under ``_refs/`` could move a tag, forge a branch or fail every ref listing with a child
      prefix. The Deny is load-bearing, measured (LH-469, #594): the staged Allow also matches
      ``<n>.manifest-<uuid>.manifest``, a name pylance reads as final, and a session policy's explicit Deny
      overrides any Allow on RustFS and on AWS S3 alike (Deny statements are evaluated first). The
      ``tree/`` grant costs 20 characters of the table prefix a write credential fits within a 2,048-byte
      session policy, the limit RustFS and AWS both enforce (109 to 89 at a 63-character bucket, counted on
      the compact policy :class:`WebIdentityVendor` sends; a KMS key's grant costs 25 more, to 64, [[LH-262]]).
    * ``maintain`` (``can_vend_maintain``, the maintenance identity alone): get, put, delete and abort-upload on ``<prefix>/*``: compaction
      commits versions and cleanup deletes them, and an object store cannot tell a rewrite from a write.

    WHAT A SESSION POLICY CANNOT REFUSE: a ``PutObject`` over an existing key. A writer's ``data/*`` grant
    can therefore overwrite a committed data file's bytes in place; Lance's own commit is put-if-not-exists
    (``lance_docs/lance/format/table/transaction.md``), but no policy condition makes a data-file put one. As an STS *session* policy
    this can only RESTRICT the catalog's role (intersection-only), never widen it.

    ``bases`` are the base paths the table's manifest declares, granted READ and never write, at either
    tier, and only when :func:`_base_is_sanctioned` allows it — inside the table's own vended scope, or
    one table's directory beneath an entry of the operator's ``sanctioned_bases`` allowlist. A base holding IAM
    policy syntax is granted nothing: no statement can name it without IAM reading the syntax, and inside the table's own scope the
    table's statements reach it already ([[LH-437]]). A table whose fragments carry a ``base_id``
    resolves them THROUGH those paths, so a credential that cannot read them is scoped to less than the
    table actually is — measured 2026-09-08 as 69 datasets a tick refused compaction because the
    maintainer could not probe a declared base (§ H12).

    ``kms_key`` is the key ARN a vend tells its writer to encrypt with (``vending.encryption``). The session may use that
    key alone, and only through S3: ``kms:Decrypt`` at every tier, since a reader GETs what was encrypted with it, and
    ``kms:GenerateDataKey`` as well at the write and maintain tiers, which PUT. Without the grant every SSE-KMS read and
    write through the credential is refused, since the session can only narrow the catalog's own policy ([[LH-262]]).

    ``sanctioned_bases`` defaults to EMPTY, which sanctions nothing foreign: a caller that has not wired
    the operator's allowlist grants only bases inside the table's own scope. Fail-closed is the right
    empty case here because the value being defaulted is a permission.

    READ ONLY is the whole of the privilege being added, and the asymmetry is deliberate: the table
    reads through the base, it does not own it, and a write grant on another dataset's root is exactly
    the blast radius vending exists to bound. ``data_directories`` are the one exception, at the table's own
    ``maintain`` tier: those of ``bases`` the table's record holds as its own data directory
    (:class:`VendableBases`), where a maintainer also deletes, because Lance's cleanup is root-scoped and leaves
    every file it stranded there ([[LH-286]]). A branch-maintain credential gets no delete: a branch's files
    there are its table's to reclaim.

    One grant shape serves both base layouts, and the spec is why (``lance_docs/lance/format/table/layout.md`` § Base Path
    System): for ``is_dataset_root`` the files sit under the base's ``data/``/``_deletions/``/
    ``_indices/``, and for a plain base "the base path points directly to the file directory" — both
    UNDER the base path, so ``<base>/*`` covers each without the policy having to read the flag. It also
    covers ``_versions/``, which is what the maintainer's dataset-root probe actually asks for.

    Raises:
        ValueError: if ``prefix`` holds IAM policy syntax (:data:`IAM_POLICY_SYNTAX`) — see
            :func:`_reject_iam_policy_syntax` — or if a base names a bucket root rather than a location
            inside one, which would widen the credential to that whole bucket
            (:func:`_reject_a_base_that_is_not_a_location`).
    """
    _reject_iam_policy_syntax("prefix", prefix)
    prefix = prefix.rstrip("/")
    # A BRANCH IS A PREFIX, and the format says which one ([[LH-055]]). `lance_docs/lance/format/table/branch_tag.md` puts a branch's
    # files at `{dataset_root}/tree/{branch_name}/` and states the name is used "as is to form the path,
    # which means `/` would create a logical subdirectory (e.g. `bugfix/issue-123`)" — so the branch name
    # is joined verbatim and a `/` inside it is intended, not an attack.
    #
    # `..` IS the attack, and it is the one thing a session policy must never permit: the whole point of
    # scoping to `<prefix>/*` is that the credential cannot leave the table, and a climbing name would
    # rewrite the resource ARN to somewhere else in the bucket.
    #
    # TYPED AS A CLIENT ERROR, unlike the prefix rejection above, and the difference is who supplied the
    # value. A branch is CALLER input, so the estate's one rule applies: raise a `lance_namespace` typed
    # error and let `ns_errors.install_problem_handlers` translate it (`InvalidInput` -> 400, RFC 9457
    # problem+json, the code clients dispatch on). A bare `ValueError` reached the door as 500
    # `InternalError` — measured live 2026-09-21 — which says the catalog is broken when the caller simply
    # sent a bad name. The prefix stays a `ValueError`: the catalog minted it as a uuid or admitted it as a
    # registered location, which refuses the syntax. A base is its table's writer's, judged by the base judge
    # before it reaches this function (`require_vendable_bases`), and one holding the syntax is dropped below.
    branch = branch.strip("/")
    if branch:
        if held := held_iam_policy_syntax(branch):
            raise InvalidInputError(f"branch {branch!r} holds IAM policy syntax {held}, which a vended policy cannot name")
        if ".." in branch.split("/"):
            raise InvalidInputError(f"branch {branch!r} may not traverse out of the table's prefix")
    # READ-ONLY ON MAIN, WRITE-ONLY ON THE BRANCH — the LanceDB blog's branching post
    # (https://www.lancedb.com/blog/branching-and-shallow-clone) names this as the governance isolation the `tree/` layout exists to give. Main stays
    # READABLE because a branch manifest references its parent's fragments through a base pointing at
    # the dataset root; a credential that could not read them would be scoped to less than the branch is.
    #
    # A READ tier needs no split: it is already read-everywhere-in-scope, and a second statement
    # granting nothing new is one more thing to get wrong.
    root = f"{bucket}/{prefix}" if prefix else bucket
    obj_actions = list(_MAINTAIN_ACTIONS if tier == "maintain" and not branch else _READ_ACTIONS)
    list_prefixes = [f"{prefix}/*"] if prefix else ["*"]
    obj_resource = f"arn:aws:s3:::{root}/*"
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
    # THE BRANCH'S OWN DIRECTORIES, never `tree/<branch>/*` ([[LH-203]]). The format lays a nested
    # branch inside its parent's directory (`a/b` at `tree/a/b/`), so a string-prefix grant on
    # `tree/a/*` was a write credential for every branch named under `a`. Naming directories
    # (`branch_layout.BRANCH_FILE_DIRS`) stops at `a`'s own files.
    ref_root = f"{root}/{BRANCH_CONTAINER}/{branch}" if branch else root
    if tier == "write":
        # The same rung on a branch as on main, scoped to the ref the writer writes; the table's own credential also
        # stages every branch's commits, because a stock client opens a branch with the table's DescribeTable.
        staged = [f"arn:aws:s3:::{ref_root}/{glob}" for glob in WRITER_PUT_GLOBS]
        statements.append(
            {
                "Sid": "StagedCommit",
                "Effect": "Allow",
                "Action": list(_APPEND_ACTIONS),
                "Resource": staged if branch else [*staged, f"arn:aws:s3:::{root}/{BRANCH_STAGING_GLOB}"],
            }
        )
        statements.append(
            {"Sid": "NeverAFinalManifest", "Effect": "Deny", "Action": ["s3:PutObject"], "Resource": f"arn:aws:s3:::{ref_root}/{FINAL_MANIFEST_GLOB}"}
        )
    elif tier == "maintain" and branch:
        statements.append(
            {
                "Sid": "BranchObjects",
                "Effect": "Allow",
                "Action": list(_MAINTAIN_ACTIONS),
                "Resource": [f"arn:aws:s3:::{ref_root}/{directory}/*" for directory in BRANCH_FILE_DIRS],
            }
        )
    if kms_key:
        statements.append(
            {
                "Sid": "KmsAccess",
                "Effect": "Allow",
                "Action": list(_KMS_READ_ACTIONS if tier == "read" else _KMS_WRITE_ACTIONS),
                "Resource": kms_key,
                # Only through S3: `kms:ViaService` names the service calling KMS on the session's behalf, so the
                # credential's holder cannot call KMS with the platform's key directly.
                "Condition": {"StringLike": {"kms:ViaService": "s3.*"}},
            }
        )
    n = 0
    for base in bases:
        if held_iam_policy_syntax(base):
            # DROPPED, as an unsanctioned base is below and for the same reason: a manifest is writer-chosen, and one
            # base must not make a table un-vendable ([[LH-437]]). Inside the table's scope the table's statements reach
            # it already; outside it, the vend doors answered server-mediated before asking (`unsanctioned_bases`).
            log.warning("vend_base_path_iam_policy_syntax", extra={"base": base, "table_bucket": bucket, "table_prefix": prefix})
            continue
        base_bucket, base_prefix = split_s3_location(base)
        base_prefix = base_prefix.rstrip("/")
        _reject_a_base_that_is_not_a_location(base, base_prefix)
        if not _base_is_sanctioned((base_bucket, base_prefix), (bucket, prefix), sanctioned_bases):
            # DROPPED, not raised, unlike the bucket-root guard above: a manifest is writer-chosen, and one
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
        reclaimed_here = tier == "maintain" and not branch and base in data_directories
        statements.append(
            {
                "Sid": f"BaseObjects{n}",
                "Effect": "Allow",
                "Action": list(_DATA_DIRECTORY_MAINTAIN_ACTIONS if reclaimed_here else _READ_ACTIONS),
                "Resource": f"arn:aws:s3:::{base_bucket}/{base_prefix}/*" if base_prefix else f"arn:aws:s3:::{base_bucket}/*",
            }
        )
        n += 1
    return {"Version": "2012-10-17", "Statement": statements}


def vend_or_refuse(
    vendor: CredentialVendor,
    *,
    table_location: str,
    tier: Tier,
    session_name: str,
    bases: Sequence[str],
    data_directories: Sequence[str] = (),
    branch: str = "",
) -> VendedSession | None:
    """``vendor.vend``, with a failed exchange answered as the spec's typed error rather than a bare 500.

    Every door that mints a credential calls this, so a refused exchange means one thing at each of them. The token
    is the catalog's own, so a refusal is never the caller's fault: it is the store's configuration or an outage, and
    a 401 would send an authorized caller into a futile re-login loop.

    Raises:
        ServiceUnavailableError: The catalog's token could not be read, the store refused the exchange, or the store
            could not be reached.
    """
    try:
        return vendor.vend(table_location=table_location, tier=tier, session_name=session_name, bases=bases, data_directories=data_directories, branch=branch)
    except IdentityTokenUnavailableError as exc:
        raise ServiceUnavailableError("the catalog's projected token could not be read, so no credential was vended") from exc
    except ClientError as exc:
        log.warning("vend_exchange_refused", extra={"code": exc.response.get("Error", {}).get("Code"), "location": table_location})
        raise ServiceUnavailableError("the object store refused the catalog's credential exchange") from exc
    except BotoCoreError as exc:
        raise ServiceUnavailableError("credential vending backend unavailable") from exc


class ModeBVendor:
    """No vending: data flows through the catalog's server-mediated endpoints."""

    def vend(
        self, *, table_location: str, tier: Tier, session_name: str, bases: Sequence[str] = (), data_directories: Sequence[str] = (), branch: str = ""
    ) -> VendedSession | None:
        return None


class WebIdentityVendor:
    """STS ``AssumeRoleWithWebIdentity`` with the catalog's own token + an inline session policy → a short-TTL, per-table credential.

    The session is bounded twice: by the policy the store grants the catalog's subject (on RustFS the canned policy
    named by the token's ``sub``), and by the session policy :func:`build_session_policy` writes for the table and
    tier. A session policy can only restrict, so a vend never reaches beyond the catalog's own grant. Each vend is
    its own exchange, never the catalog's own session, so what a client holds is scoped to the table it was vended
    for, and its name (:func:`role_session_name`) says who it was vended for.

    ``exchange`` is :func:`service_kit.governed.store_session.exchange` and is injectable for tests.
    """

    def __init__(
        self,
        *,
        identity: WebIdentity,
        store_endpoint: str | None = None,
        ttl_seconds: int = 900,
        encryption: EncryptionAtRest | None = None,
        sanctioned_bases: Sequence[str] = (),
        exchange: Callable[..., IssuedSession] = store_session.exchange,
    ) -> None:
        #: Travels with every vend: a direct writer has no other channel to learn it (§ J5).
        self._encryption = encryption or EncryptionAtRest()
        #: The operator's allowlist of legitimate FOREIGN base paths. Deployment config rather than a `vend` argument:
        #: which buckets belong to this lakehouse is not a per-request question, and a per-request one would be
        #: answerable by the caller whose manifest is the untrusted input.
        self._sanctioned_bases = tuple(sanctioned_bases)
        self._identity = identity
        #: The object-store address the credential is used at, which a client off the cluster reaches and the STS
        #: endpoint need not be ([[LH-489]]). ``None`` is the STS endpoint's own address.
        self._store_endpoint = identity.endpoint if store_endpoint is None else store_endpoint
        self._ttl = ttl_seconds
        self._exchange = exchange

    def vend(
        self, *, table_location: str, tier: Tier, session_name: str, bases: Sequence[str] = (), data_directories: Sequence[str] = (), branch: str = ""
    ) -> VendedSession | None:
        bucket, prefix = split_s3_location(table_location)
        policy = build_session_policy(
            bucket,
            prefix,
            tier,
            bases,
            sanctioned_bases=self._sanctioned_bases,
            data_directories=data_directories,
            branch=branch,
            kms_key=self._encryption.kms_key_id,
        )
        text = json.dumps(policy, separators=(",", ":"))
        if len(text) > SESSION_POLICY_LIMIT:
            # Refused here, naming the cause: past the limit the store refuses the exchange, which the caller would read as
            # an outage to retry, when no retry can shorten the table's location or its bases ([[LH-262]]).
            raise SessionPolicyTooLong(
                f"the {tier} session policy for {table_location} is {len(text)} characters, past the {SESSION_POLICY_LIMIT:,} STS accepts: "
                "its location, bases or KMS key are too long for a direct credential",
                characters=len(text),
            )
        issued = self._exchange(self._identity, session_name=session_name, duration_seconds=self._ttl, policy=text)
        return VendedSession(
            key_pair=issued.key_pair,
            endpoint=self._store_endpoint or None,
            region=self._identity.region,
            encryption=self._encryption,
            expires_at_millis=int(issued.expiration.timestamp() * 1000),
        )


def make_vendor(
    mode: VendingMode,
    *,
    identity: WebIdentity | None = None,
    store_endpoint: str | None = None,
    ttl_seconds: int = 900,
    encryption: EncryptionAtRest | None = None,
    sanctioned_bases: Sequence[str] = (),
) -> CredentialVendor:
    """Build the configured :class:`CredentialVendor`.

    Raises:
        ValueError: for ``web_identity`` mode without the catalog's identity, or an unknown mode.
    """
    if mode == "mode_b":
        return ModeBVendor()
    if mode == "web_identity":
        if identity is None:
            raise ValueError("web_identity vending exchanges the catalog's own token, and this catalog holds none")
        return WebIdentityVendor(
            identity=identity, store_endpoint=store_endpoint, ttl_seconds=ttl_seconds, encryption=encryption, sanctioned_bases=sanctioned_bases
        )
    assert_never(mode)
