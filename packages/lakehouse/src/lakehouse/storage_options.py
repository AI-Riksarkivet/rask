"""The storage options a service signs with, and the S3 filesystem they build: the lakehouse's one credential guard.

One home for the two places a store credential becomes something that signs. :class:`StorageOptions` carries a flat
lance-style ``storage_options`` map with its credential lifted into a :class:`~lakehouse.governed.store_credential.S3KeyPair`
whose secret and session token are ``SecretStr`` ([[LH-543]]), so a repr, a log line or an error that echoes the options
carries neither, and a map naming half a credential, or one field under two spellings that disagree, is refused rather
than read as no credential. :func:`s3_filesystem` builds the pyarrow ``S3FileSystem`` from them with every half of the
credential forwarded: pyarrow signs with the default credential chain for anything it is not given, so a vended credential
whose session token were dropped would sign as the pod's own broader role. A lakehouse service and a client holding a vended
credential build both through here, so neither failure can come back in a copy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import pyarrow.fs as pafs
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from lakehouse.endpoint_scheme import allow_http_for, endpoint_scheme
from lakehouse.governed.store_credential import S3KeyPair


if TYPE_CHECKING:
    from lakehouse.storage import S3Client


#: The two spellings of each credential field. :meth:`StorageOptions.in_clear` emits the ``aws_``-prefixed form (it says
#: why the bare one loses to the ambient environment), but maps also arrive from the
#: wire (a vending response, a client's own options), so :meth:`StorageOptions.from_flat` reads both: a reader of one
#: spelling would not error, it would fall back to the default credential chain, which is the pod's own broader role
#: rather than the scoped credential it was handed. Failing OPEN, silently.
_CREDENTIAL_ALIASES: tuple[tuple[str, str], ...] = (
    ("aws_access_key_id", "access_key_id"),
    ("aws_secret_access_key", "secret_access_key"),
    ("aws_session_token", "session_token"),
)

#: Every key a credential may be spelled under. None is ever an option beside the key pair: object_store reads either
#: spelling, so options carrying ``access_key_id`` beside a key pair would name two identities, and which one signs is
#: not defined.
CREDENTIAL_KEYS: frozenset[str] = frozenset(alias for aliases in _CREDENTIAL_ALIASES for alias in aliases)

#: Where the store is, the region it signs for and its addressing, each under every spelling object_store reads, the
#: canonical one first. The canonical spelling is the name Lance reads an ``AWS_*`` variable into, and it reads one only
#: where the options do not hold that name, so options under it are never joined by the environment's; under a bare
#: spelling both land and the one applied last wins, in hash order ([[LH-238]]). Measured 2026-11-08 on pylance 13.0.0,
#: ten fresh processes under ``AWS_ENDPOINT_URL``, ``AWS_REGION`` and ``AWS_VIRTUAL_HOSTED_STYLE_REQUEST``: the bare
#: spellings sent 5 of 10 to the ambient endpoint, half the requests signed for the ambient region or addressed
#: virtual-hosted; the canonical ones held in 10 of 10 (`test_explicit_credentials_beat_the_ambient_environment.py`).
_OPTION_ALIASES: Final[tuple[tuple[str, ...], ...]] = (
    ("aws_endpoint", "endpoint", "aws_endpoint_url", "endpoint_url"),
    ("aws_region", "region"),
    ("aws_virtual_hosted_style_request", "virtual_hosted_style_request"),
)
_ALIASED_KEYS: Final = frozenset(key for aliases in _OPTION_ALIASES for key in aliases)
ENDPOINT: Final = _OPTION_ALIASES[0][0]
REGION: Final = _OPTION_ALIASES[1][0]
VIRTUAL_HOSTED: Final = _OPTION_ALIASES[2][0]
#: The S3 service's own endpoint, a key of its own to object_store that outranks :data:`ENDPOINT`, which Lance fills from
#: ``AWS_ENDPOINT_URL_S3`` wherever the options do not hold it, so it is set beside the endpoint rather than as one of its
#: spellings. Measured 2026-11-08 on pylance 13.0.0 against the dev estate's vend: ten fresh processes under
#: ``AWS_ENDPOINT_URL_S3`` all sent the vended credential to the ambient endpoint; with this key set to the vended
#: endpoint, all ten reached the vended store.
S3_ENDPOINT: Final = "aws_endpoint_url_s3"
#: S3 Express One Zone resolves an endpoint of its own from the bucket, so it is pinned off wherever an endpoint is named:
#: under an ambient ``AWS_S3_EXPRESS=true`` the same ten processes could open nothing.
S3_EXPRESS: Final = "aws_s3_express"
#: Pinned off wherever a key pair signs: under an ambient ``AWS_SKIP_SIGNATURE=true`` the same ten processes sent every
#: request unsigned, so the credential they held reached no store.
_SKIP_SIGNATURE: Final = "aws_skip_signature"
#: The region a client signs for when the options name none: never the ambient ``AWS_REGION``, which is another value in
#: another process. It is the region RustFS serves (`rustfs@1.0.3:crates/config/src/constants/app.rs:289-292`).
DEFAULT_REGION: Final = "us-east-1"

#: Pins the credential Lance signs with to the key pair the options carry ([[LH-219]]): measured on pylance 13.0.0, with
#: it a map that holds no key pair is refused at once ("aws_provider_scheme=token requires aws_access_key_id and
#: aws_secret_access_key to be set"), where without it Lance walks the default chain to instance metadata, which on a
#: cloud node signs as the node's role. It does not stop an ambient ``AWS_ACCESS_KEY_ID`` standing in for a missing pair,
#: so it is derived from the key pair, set wherever one signs, and never read from a map.
_PROVIDER_SCHEME: Final = "aws_provider_scheme"


class StorageOptions(BaseModel):
    """How a service reaches the object store: the key pair it signs with, and every other ``storage_options`` key ([[LH-543]]).

    The secret access key and the session token are ``SecretStr`` (:class:`S3KeyPair`), so a repr, a str, a model dump, a
    log line or an error that echoes these options, or any model holding them (a history fence, a claim store, a base
    registry, an expiry target), carries neither, as a vend carries neither ([[LH-072]]). They leave only at the call
    that signs: :meth:`in_clear` builds the flat map Lance and the ``dir`` namespace take, and
    :meth:`credential_in_clear` hands boto3 and pyarrow the pair.
    """

    #: ``hide_input_in_errors``: a refusal of these options echoes none of their values.
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    #: ``None`` signs with the ambient credential chain: a local root, or a store sharing the deployment's identity.
    key_pair: S3KeyPair | None = None
    #: Every other key, as `lance_docs/lance/guide/object_store.md` names it (endpoint, region, path-style, timeouts,
    #: encryption); never a credential's.
    options: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _parse_a_flat_map(cls, data: object) -> object:
        """A flat ``storage_options`` map handed where these options are expected is parsed as :meth:`from_flat` parses it,
        and a credential that cannot be signed with reaches :meth:`_holds_no_credential` masked.

        Measured 2026-10-31 on a holder given one: under pydantic's default its keys were unknown fields and dropped, the
        credential with them, so the holder signed with the ambient chain; refused as extra fields, the holder's error
        printed the map's secret and token, since the outermost model decides what an error echoes. So no refusal of these
        options is raised over a credential's value.
        """
        if not isinstance(data, Mapping):
            return data
        if data.keys() <= cls.model_fields.keys():
            options = data.get("options")
            return {**data, "options": _masked(options)} if isinstance(options, Mapping) else data
        try:
            key_pair, options = _split(data)
        except ValueError:
            return {"options": _masked(data)}
        return {"key_pair": key_pair, "options": options}

    @field_validator("options")
    @classmethod
    def _holds_no_credential(cls, options: dict[str, str]) -> dict[str, str]:
        if held := sorted(CREDENTIAL_KEYS & options.keys()):
            raise ValueError(f"{', '.join(held)}: a credential is one whole key pair, and only the key pair holds it")
        return _canonical({key: value for key, value in options.items() if key != _PROVIDER_SCHEME})

    @classmethod
    def from_flat(cls, flat: Mapping[str, str]) -> StorageOptions:
        """The options a flat ``storage_options`` map names, its credential, under either spelling, lifted into the key pair.

        Raises:
            ValueError: The map names part of a credential, or one of its fields under two spellings that disagree.
        """
        key_pair, options = _split(flat)
        return cls(key_pair=key_pair, options=options)

    @property
    def endpoint(self) -> str | None:
        """The store address these options name; ``None`` for AWS proper, whose regional endpoint the client resolves."""
        return self.options.get(ENDPOINT) or None

    @property
    def region(self) -> str | None:
        """The region these options sign for; ``None`` leaves it to the client building them."""
        return self.options.get(REGION) or None

    def with_options(self, extra: Mapping[str, str]) -> StorageOptions:
        """These options with ``extra``'s keys set over them, under the same key pair, each under its canonical spelling.

        Raises:
            ValueError: ``extra`` names one setting under two spellings that disagree.
        """
        return StorageOptions(key_pair=self.key_pair, options={**self.options, **_canonical(extra)})

    def with_endpoint(self, endpoint: str) -> StorageOptions:
        """These options at ``endpoint``, with the plaintext permit its scheme implies and nothing else can grant.

        ``allow_http`` is derived here and nowhere else (``endpoint_scheme.allow_http_for``): ``"true"`` exactly for an
        ``http`` scheme, so a TLS store never holds the permit and a plaintext one never lacks it.
        """
        return self.with_options({ENDPOINT: endpoint, "allow_http": allow_http_for(endpoint)})

    def in_clear(self) -> dict[str, str]:
        """The flat ``storage_options`` map Lance takes, the key pair in clear at the ``aws_`` spelling.

        Called only at the call that signs: a Lance open, write or commit, and the ``dir`` namespace's ``storage.``
        properties (`lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md:21`). A session token is omitted
        rather than set empty: object_store treats a present-but-empty token as a token and the request is refused.

        THE KEYS ARE ``aws_``-PREFIXED BECAUSE THE BARE ONES DO NOT DISPLACE THE ENVIRONMENT. Every fleet pod exports
        ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` (the root key), and with the bare spellings object_store BLENDS
        the two sources — one half from the environment, one from here — and signs with a pair belonging to neither
        identity. Measured in-cluster 2026-09-03: the ingest worker's vended credential failed every write with
        ``403 SignatureDoesNotMatch`` while the identical options read the identical table successfully from a process
        with no AWS_* environment; exporting those two variables locally reproduced it exactly, and the
        ``aws_``-prefixed form read it successfully with them still set. The failure is doubly hidden: it reads as an
        expired or broken credential rather than a precedence bug, and no test process has an ambient AWS_*
        environment, so the spelling that fails in every pod passes every unit test.

        A key pair is pinned as the only credential Lance may sign with (``aws_provider_scheme``, :data:`_PROVIDER_SCHEME`),
        and every request is signed with it (:data:`_SKIP_SIGNATURE`). A named endpoint is pinned as the S3 service's own
        too (:data:`S3_ENDPOINT`) with S3 Express off (:data:`S3_EXPRESS`), here rather than where the endpoint is set,
        so options read from the wire are pinned as the ones this module builds are ([[LH-238]]).
        """
        options = dict(self.options)
        if endpoint := self.endpoint:
            options |= {S3_ENDPOINT: endpoint, S3_EXPRESS: "false"}
        if (pair := self.key_pair) is None:
            return options
        credential = {
            "aws_access_key_id": pair.access_key_id,
            "aws_secret_access_key": pair.secret_access_key.get_secret_value(),
            _PROVIDER_SCHEME: "token",
            _SKIP_SIGNATURE: "false",
        }
        if pair.session_token is not None and (token := pair.session_token.get_secret_value()):
            credential["aws_session_token"] = token
        return {**options, **credential}

    def credential_in_clear(self) -> tuple[str | None, str | None, str | None]:
        """The (access key, secret, session token) these options sign with, in clear for the boto3 or pyarrow client being
        built with them.

        ``None`` for a field the options carry no value for — meaning "use the ambient chain for this one", which is what a
        store sharing the deployment's credentials deliberately wants.
        """
        if (pair := self.key_pair) is None:
            return None, None, None
        session_token = pair.session_token.get_secret_value() if pair.session_token is not None else None
        return pair.access_key_id or None, pair.secret_access_key.get_secret_value() or None, session_token or None


def _split(flat: Mapping[str, str]) -> tuple[S3KeyPair | None, dict[str, str]]:
    """``flat``'s credential, under either spelling, as a key pair, and every other key.

    Raises:
        ValueError: ``flat`` names part of a credential, or one field under two spellings that disagree. Half a pair
            signs nothing, and read as no credential the ambient chain, the pod's own broader role, would sign in its
            place; two spellings name two identities. The message names keys, never their values.
    """
    access_key, secret_key, session_token = (_one_field(flat, aliases) for aliases in _CREDENTIAL_ALIASES)
    options = {key: value for key, value in flat.items() if key not in CREDENTIAL_KEYS}
    if access_key is None and secret_key is None and session_token is None:
        return None, options
    if access_key is None or secret_key is None:
        named = ", ".join(sorted(key for key in CREDENTIAL_KEYS if flat.get(key)))
        raise ValueError(f"{named} name part of a credential: a key id and its secret sign only together")
    token = None if session_token is None else SecretStr(session_token)
    return S3KeyPair(access_key_id=access_key, secret_access_key=SecretStr(secret_key), session_token=token), options


def _one_field(flat: Mapping[str, str], aliases: tuple[str, str]) -> str | None:
    """The value ``flat`` names for one credential field under either of its ``aliases``, ``None`` for none.

    Raises:
        ValueError: The two spellings name different values.
    """
    values = {flat[key] for key in aliases if flat.get(key)}
    if len(values) > 1:
        raise ValueError(f"{' and '.join(aliases)} name different values: two identities, and which one signs is not defined")
    return next(iter(values), None)


def _masked(options: Mapping[str, str]) -> dict[str, str]:
    """``options`` with every credential key's value masked, for a refusal that must not echo it."""
    return {key: "**********" if key in CREDENTIAL_KEYS else value for key, value in options.items()}


def _canonical(options: Mapping[str, str]) -> dict[str, str]:
    """``options`` with each setting of :data:`_OPTION_ALIASES` under its canonical spelling.

    Raises:
        ValueError: Two spellings of one setting name different values: which one Lance applies is decided by hash order.
    """
    canonical = {key: value for key, value in options.items() if key not in _ALIASED_KEYS}
    for aliases in _OPTION_ALIASES:
        values = {options[key] for key in aliases if key in options}
        if len(values) > 1:
            raise ValueError(f"{' and '.join(aliases)} name different values: which one Lance applies is decided by hash order")
        if values:
            canonical[aliases[0]] = values.pop()
    return canonical


def on_s3(location: str) -> bool:
    """Whether ``location`` is on S3: the one answer to which client reaches it.

    A location on S3 is reached through :func:`object_client` or :func:`s3_filesystem`, signed by the options' own key
    pair whether or not they name an endpoint, since an options map naming none is AWS proper and its regional endpoint.
    Any other location resolves through pyarrow's own filesystem for its scheme, which for ``s3://`` would sign with the
    pod's ambient credential chain instead ([[LH-572]]).
    """
    return location.startswith("s3://")


def object_client(storage_options: StorageOptions) -> S3Client:
    """The storage seam's S3 client for these options, signed by their whole key pair, at their endpoint and region.

    ``lakehouse.storage.s3_client`` owns, in one place, what a hand-rolled client forfeits: path-style addressing (RustFS
    refuses virtual-hosted signing with 403 ``SignatureDoesNotMatch``), s3v4, adaptive retries, and connect and read
    timeouts, which matter on the synchronous path of every registry write, where botocore's default waits forever. The
    region is the options' own or :data:`DEFAULT_REGION`, never the ambient ``AWS_REGION``. The session token is forwarded
    whenever the options carry one: a vended credential without it is not a weaker credential but an invalid one, and boto3
    would fall back to whatever ambient chain it finds, broader rights than the vend scoped. ``lakehouse.storage`` is imported at
    call time so a test replaces the seam rather than this wrapper, and a local root needs no S3 stack at all.
    """
    from lakehouse.storage import s3_client

    access_key, secret_key, session_token = storage_options.credential_in_clear()
    region = storage_options.region or DEFAULT_REGION
    return s3_client(storage_options.endpoint, access_key=access_key, secret_key=secret_key, session_token=session_token, region=region)


def s3_filesystem(storage_options: StorageOptions, *, allow_bucket_creation: bool = False) -> pafs.S3FileSystem:
    """An ``S3FileSystem`` from lance-style storage options, reaching the endpoint over the scheme
    ``endpoint_scheme`` reads — the rule ``allow_http`` follows too, so pyarrow and Lance agree on
    which stores are plaintext. pyarrow accepts only a lowercase ``http``/``https`` (measured on pyarrow
    25.0.0: ``scheme="HTTP"`` raises ``ArrowInvalid``), and an empty endpoint is AWS proper over TLS.

    The override keeps the endpoint's PATH, less a trailing slash: Lance and boto3 address
    ``<path>/<bucket>/<key>`` under an endpoint with one, and so does pyarrow given ``host/path``
    (measured on pylance 12.0.0 and pyarrow 25.0.0 against a local listener). A host-only override
    reaches ``/<bucket>/<key>``, a different root of the same store.

    The ``session_token`` and the region are forwarded for the same reasons :func:`object_client` forwards them, and the
    stakes are higher here: pyarrow falls back to the default credential chain for anything it was not
    given, so a half-forwarded vended credential can sign with the POD's own role — broader rights than
    the catalog scoped, not narrower. Dropping it fails open.
    """
    endpoint = storage_options.endpoint or ""
    parts = urlsplit(endpoint)
    access_key, secret_key, session_token = storage_options.credential_in_clear()
    return pafs.S3FileSystem(
        access_key=access_key,
        secret_key=secret_key,
        session_token=session_token,
        endpoint_override=(parts.netloc + parts.path).rstrip("/"),
        scheme=endpoint_scheme(endpoint),
        region=storage_options.region or DEFAULT_REGION,
        allow_bucket_creation=allow_bucket_creation,
    )
