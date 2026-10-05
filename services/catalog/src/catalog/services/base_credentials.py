"""Give each data base ITS credential, at every open and every write, rather than the estate's ([[LH-067]], [[LH-273]]).

A data base is an approved S3 location that may live under another identity than the estate's, and
pylance carries a base's own object-store options only as ``base_store_params`` — runtime-only, "not
persisted to the manifest" (pylance 12.0.0 docstring), keyed by BASE PATH URI. Measured on pylance
12.0.0 over a moto server enforcing per-key bucket policies: a read of a table whose fragments sit on a
base under a second key, opened with the estate options alone, fails "lacked the necessary privileges";
opened with that base's own entry it returns every row. The key must be the manifest's exact base path:
the same path with a trailing slash, or its parent, is not matched and the read fails the same way.

REFERENCES, NEVER MATERIAL. What an operator configures is base URI -> secret NAME
(``LANCE_MULTIBASE_BASE_CREDENTIAL_REFS``). The material is fetched through the Dapr secret-store door
the catalog already uses for its own S3 secret, so nothing secret is in configuration, in a record, or
in an environment variable.

A CONFIGURED BASE COVERS WHAT LIES BENEATH IT. A table's manifest registers its own directory under the
configured base, so the reference applies to every declared base path at or under the configured URI,
on a path boundary and in the same store (:func:`service_kit.lakehouse.base_refs.location_in_store`).

A CREDENTIAL IS NOT AN ADDRESS. A referenced base keeps the estate's endpoint unless something else
moves it; swapping the key must not silently relocate where the base is read and written.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING

from lance_namespace import ServiceUnavailableError
from pydantic import BaseModel, Field

from catalog.services import warehouse_credentials
from service_kit.lakehouse.base_refs import location_in_store
from service_kit.lakehouse.objectfs import CREDENTIAL_KEYS


if TYPE_CHECKING:
    from catalog.core.config import Settings


class BaseCredentialUnavailableError(ServiceUnavailableError):
    """A data base's credential reference did not resolve, so the table was not opened (503, retryable).

    A 5xx's ``detail`` is redacted estate-wide (``ns_errors.problem_detail``), so what the caller needs to
    act on rides as RFC 9457 extension members: the base, the reference and the store — operator
    configuration names, never a secret value.
    """

    problem_extra: dict[str, object]

    def __init__(self, message: str, *, base: str, reference: str, store: str) -> None:
        super().__init__(message)
        self.problem_extra = {"data_base": base, "credential_reference": reference, "secret_store": store}


def compose_base_store_params(
    *,
    bases: Iterable[str],
    storage_options: Mapping[str, str],
    refs: Mapping[str, str],
    resolve: Callable[..., Mapping[str, str] | None],
    store: str,
    field: str,
) -> dict[str, dict[str, str]] | None:
    """``base_store_params`` for ``lance.write_dataset`` / ``lance.dataset`` over the declared ``bases``.

    ``None`` when no base lies under a referenced one: pylance falls back to the top-level options for
    a base with no entry, so the open is then byte-identical to passing nothing, and a caller can tell
    from ``None`` alone that no second identity is involved. Otherwise EVERY base gets an entry — the
    estate's options for an unreferenced one — so "which options does this base use" is answerable from
    the map alone rather than from an upstream fallback.

    ``refs`` is the estate-wide map, so a reference naming a base this table does not declare is the
    ordinary case and is ignored; whether a reference names an approved base at all is judged where the
    map is parsed (``Settings.multibase_base_credential_ref_map``).

    Raises when a reference does not RESOLVE: falling back to the estate key would read or write the
    caller's bytes in a store under an identity nobody chose, and report success.

    Raises:
        BaseCredentialUnavailableError: The secret store did not answer for a reference, or answered
            without the credential (503, retryable once the store holds it).
    """
    params: dict[str, dict[str, str]] = {}
    referenced = False
    for path in bases:
        ref = next((secret for configured, secret in refs.items() if secret and location_in_store(configured, path)), "")
        if not ref:
            params[path] = dict(storage_options)
            continue
        try:
            pair = resolve(store=store, ref=ref, field=field)
        except RuntimeError as exc:
            raise BaseCredentialUnavailableError(
                f"the credential reference {ref!r} for data base {path!r} could not be resolved from secret store {store!r}, "
                "so the table was not opened under any other identity",
                base=path,
                reference=ref,
                store=store,
            ) from exc
        if not pair:
            raise ValueError(f"per-base credential reference {ref!r} for {path!r} resolved to nothing")
        # BOTH HALVES OR NEITHER, AND NONE OF THE ESTATE'S. A credential is a PAIR, and replacing only
        # the secret leaves the estate's key id signing with another store's secret —
        # `SignatureDoesNotMatch` on every write, measured live 2026-09-21. Every spelling of the
        # estate's key goes first: the catalog's options say `access_key_id` and the pair says
        # `aws_access_key_id`, and object_store reads both, so an entry holding the two named two
        # identities — measured over moto with per-key bucket policies, the same write landed under the
        # second key on one run and was refused under the estate key on the next. The endpoint and the
        # rest do NOT move: a credential is not an address.
        params[path] = {**{k: v for k, v in storage_options.items() if k not in CREDENTIAL_KEYS}, **dict(pair)}
        referenced = True
    return params if referenced else None


class BaseCredentials(BaseModel):
    """Which secret each configured data base's credential comes from, and the store holding it."""

    #: ``{configured base URI: secret reference}`` — names, never material.
    refs: dict[str, str] = Field(default_factory=dict)
    #: The Dapr secret store the references are fetched from.
    store: str = ""
    #: The bundle field holding the secret access key; the key id rides the same bundle.
    field: str = ""

    @classmethod
    def from_settings(cls, settings: Settings) -> BaseCredentials:
        """The credentials a catalog configured by ``settings`` opens and writes its data bases with."""
        return cls(refs=settings.multibase_base_credential_ref_map, store=settings.dapr_secret_store, field=settings.dapr_secret_s3_field)

    def store_params(self, bases: Iterable[str], storage_options: Mapping[str, str]) -> dict[str, dict[str, str]] | None:
        """:func:`compose_base_store_params` over ``bases``, resolving through the catalog's secret door."""
        if not self.refs:
            return None
        return compose_base_store_params(
            bases=bases, storage_options=storage_options, refs=self.refs, resolve=warehouse_credentials.resolve, store=self.store, field=self.field
        )
