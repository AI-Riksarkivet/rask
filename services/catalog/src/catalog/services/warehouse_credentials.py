"""Resolve the credential a warehouse NAMES, from the door the estate already uses ([[LH-067]]).

A warehouse may live in a different object store from the estate's own. Its ADDRESS is non-secret and
rides the record (`endpoint`); its CREDENTIAL cannot, under the estate's standing rule — material never
travels in a record, and a scoped static key is not a fix. So the record names a REFERENCE and the
material is fetched here, from the Dapr secret store the catalog reads its other secrets from.

WHY NOT THE CATALOG'S OWN CREDENTIAL HOLDER. `service_kit.governed.store_session` keeps ONE session, the
catalog's token exchanged at boot and renewed in the background (ADR 0170). A warehouse credential is per-RECORD
and is discovered when a request names that warehouse, so it is read on demand, and must never be
written onto settings.

HELD FOR THE STORE'S TTL, by reference ([[XC-082]]), because every table open under a warehouse names
the same one and the store is a network hop: `service_kit.governed.secret_cache.STORE_READS` serves it
for at most `SECRET_TTL_SECONDS` and then reads the store again, so a pair rotated in the store is the
one signing within the TTL, with no restart. Keyed on `(store, ref, field)` so two warehouses naming
different references cannot share an entry, and so a corrected reference is a different key rather than
a stale hit. Concurrent opens that find it expired wait on one read.

FAILS CLOSED, inherited rather than re-implemented: `fetch_required_secrets` raises when the bundle
lacks the required field, and nothing here catches it. A fall-back to the estate's own key would be
worse than an error — the write would SUCCEED, against the wrong store, with the estate's credential,
which is the blast radius per-warehouse credentials exist to contain.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Final

from service_kit.governed import secret_cache
from service_kit.governed.secret_cache import StoreRead
from service_kit.governed.secrets import fetch_required_secrets


#: The storage-options names a vended pair uses. Both come from ONE secret bundle, because a
#: credential is a pair and half of one signs nothing — `SignatureDoesNotMatch`, measured 2026-09-21.
_KEY_ID_FIELD: Final = "aws_access_key_id"

#: The fetch policy on the REQUEST path: two attempts of one second each and a quarter-second backoff,
#: so a store that cannot answer fails the open in about 2.5 s. ``fetch_dapr_secret``'s own defaults are
#: the BOOT policy, sized to wait out a seeding store; on an open they held the catalog worker in the
#: retry loop past the client's 60 s timeout — measured live 2026-10-05 against a reference whose
#: OpenBao entry was gone (Dapr 500, waits of 12.4 s, 15 s, 15 s, ... from 18:35:19 to 18:36:16Z).
_REQUEST_TIMEOUT: Final = 1.0
_REQUEST_RETRIES: Final = 2
_REQUEST_BACKOFF: Final = 0.25


def resolve(*, store: str, ref: str, field: str) -> Mapping[str, str] | None:
    """The secret material for the warehouse naming ``ref``, or ``None`` when it names none.

    ``None`` is "this record names no second store", NOT "use the estate's key". The caller decides
    what absence means — today it keeps the estate's own configured credential, because a warehouse
    without a reference is in the estate's own store. Returning that secret from HERE would make an
    unset reference indistinguishable from one that resolved, which is how a misconfigured record
    starts writing somewhere nobody intended.

    Raises:
        RuntimeError: The store did not answer, or answered without both halves of the pair. Never cached.
    """
    if not ref:
        return None
    read = StoreRead(store=store, secret=ref, fields=(field, _KEY_ID_FIELD))
    pair = secret_cache.STORE_READS.get(read, partial(_read_pair, read))
    return {"aws_secret_access_key": pair[field], _KEY_ID_FIELD: pair[_KEY_ID_FIELD]}


def _read_pair(read: StoreRead) -> dict[str, str]:
    """Both halves of the pair from one fetch, under the request path's bound."""
    field, key_id_field = read.fields
    bundle = fetch_required_secrets(read.store, read.secret, require=field, timeout=_REQUEST_TIMEOUT, retries=_REQUEST_RETRIES, backoff=_REQUEST_BACKOFF)
    # The key id rides the SAME bundle. Absent, the estate's own id stays in force and is paired with
    # a foreign secret — so its absence is refused rather than defaulted.
    key_id = bundle.get(key_id_field)
    if not key_id:
        raise RuntimeError(f"secret bundle {read.store!r}/{read.secret!r} carries {field!r} but no {key_id_field!r}; half a credential signs nothing")
    return {field: bundle[field], key_id_field: key_id}
