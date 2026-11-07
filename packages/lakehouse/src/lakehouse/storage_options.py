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
from urllib.parse import urlsplit

import pyarrow.fs as pafs
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from lakehouse.endpoint_scheme import endpoint_scheme
from lakehouse.governed.store_credential import S3KeyPair


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
        return options

    @classmethod
    def from_flat(cls, flat: Mapping[str, str]) -> StorageOptions:
        """The options a flat ``storage_options`` map names, its credential, under either spelling, lifted into the key pair.

        Raises:
            ValueError: The map names part of a credential, or one of its fields under two spellings that disagree.
        """
        key_pair, options = _split(flat)
        return cls(key_pair=key_pair, options=options)

    def reaches_by_endpoint(self, uri: str) -> bool:
        """Whether ``uri`` is an ``s3://`` location these options name an endpoint for, reached through an S3 client there;
        any other location resolves through pyarrow's own filesystem for its scheme."""
        return uri.startswith("s3://") and bool(self.options.get("endpoint"))

    def with_options(self, extra: Mapping[str, str]) -> StorageOptions:
        """These options with ``extra``'s keys set over them, under the same key pair."""
        return StorageOptions(key_pair=self.key_pair, options={**self.options, **extra})

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
        """
        if (pair := self.key_pair) is None:
            return dict(self.options)
        credential = {"aws_access_key_id": pair.access_key_id, "aws_secret_access_key": pair.secret_access_key.get_secret_value()}
        if pair.session_token is not None and (token := pair.session_token.get_secret_value()):
            credential["aws_session_token"] = token
        return {**self.options, **credential}

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


def s3_filesystem(storage_options: StorageOptions, *, allow_bucket_creation: bool = False) -> pafs.S3FileSystem:
    """An ``S3FileSystem`` from lance-style storage options, reaching the endpoint over the scheme
    ``endpoint_scheme`` reads — the rule ``allow_http`` follows too, so pyarrow and Lance agree on
    which stores are plaintext. pyarrow accepts only a lowercase ``http``/``https`` (measured on pyarrow
    25.0.0: ``scheme="HTTP"`` raises ``ArrowInvalid``), and an empty endpoint is AWS proper over TLS.

    The override keeps the endpoint's PATH, less a trailing slash: Lance and boto3 address
    ``<path>/<bucket>/<key>`` under an endpoint with one, and so does pyarrow given ``host/path``
    (measured on pylance 12.0.0 and pyarrow 25.0.0 against a local listener). A host-only override
    reaches ``/<bucket>/<key>``, a different root of the same store.

    The ``session_token`` is forwarded for the same reason ``records._s3_client`` forwards it, and the
    stakes are higher here: pyarrow falls back to the default credential chain for anything it was not
    given, so a half-forwarded vended credential can sign with the POD's own role — broader rights than
    the catalog scoped, not narrower. Dropping it fails open.
    """
    endpoint = storage_options.options["endpoint"]
    parts = urlsplit(endpoint)
    access_key, secret_key, session_token = storage_options.credential_in_clear()
    return pafs.S3FileSystem(
        access_key=access_key,
        secret_key=secret_key,
        session_token=session_token,
        endpoint_override=(parts.netloc + parts.path).rstrip("/"),
        scheme=endpoint_scheme(endpoint),
        region=storage_options.options.get("region", ""),
        allow_bucket_creation=allow_bucket_creation,
    )
