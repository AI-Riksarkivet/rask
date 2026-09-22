"""Resolve the credential a warehouse NAMES, from the door the estate already uses ([[LH-067]]).

A warehouse may live in a different object store from the estate's own. Its ADDRESS is non-secret and
rides the record (`endpoint`); its CREDENTIAL cannot, under the estate's standing rule — material never
travels in a record, and a scoped static key is not a fix. So the record names a REFERENCE and the
material is fetched here, from the same Dapr secret store the catalog already resolves its own S3
secret from.

WHY NOT `apply_dapr_secrets`. That splices the ESTATE's one secret into `Settings` during the
lifespan, and its docstring forbids reuse on this path in as many words: "never call this from a
request handler or a background task". It mutates a shared `@lru_cache`d object that every later read
resolves, which is sound exactly once, before the first request. A warehouse credential is per-RECORD
and is discovered when a request names that warehouse, so it needs a holder of its own and must never
be written back onto settings.

CACHED BY REFERENCE, because every table open under a warehouse names the same one and the store is a
network hop. Keyed on `(store, ref, field)` so two warehouses naming different references cannot share
an entry, and so a corrected reference is a different key rather than a stale hit. `cache_clear()` is
the seam a rotation would use.

FAILS CLOSED, inherited rather than re-implemented: `fetch_required_secrets` raises when the bundle
lacks the required field, and nothing here catches it. A fall-back to the estate's own key would be
worse than an error — the write would SUCCEED, against the wrong store, with the estate's credential,
which is the blast radius per-warehouse credentials exist to contain.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Final

from service_kit.governed.secrets import fetch_required_secrets


#: The storage-options names a vended pair uses. Both come from ONE secret bundle, because a
#: credential is a pair and half of one signs nothing — `SignatureDoesNotMatch`, measured 2026-09-21.
_KEY_ID_FIELD: Final = "aws_access_key_id"


@lru_cache(maxsize=256)
def resolve(*, store: str, ref: str, field: str) -> Mapping[str, str] | None:
    """The secret material for the warehouse naming ``ref``, or ``None`` when it names none.

    ``None`` is "this record names no second store", NOT "use the estate's key". The caller decides
    what absence means — today it keeps the estate's own configured credential, because a warehouse
    without a reference is in the estate's own store. Returning that secret from HERE would make an
    unset reference indistinguishable from one that resolved, which is how a misconfigured record
    starts writing somewhere nobody intended.
    """
    if not ref:
        return None
    bundle = fetch_required_secrets(store, ref, require=field)
    pair = {"aws_secret_access_key": bundle[field]}
    # The key id rides the SAME bundle. Absent, the estate's own id stays in force and is paired with
    # a foreign secret — so its absence is refused rather than defaulted.
    key_id = bundle.get(_KEY_ID_FIELD)
    if not key_id:
        raise RuntimeError(f"secret bundle {store!r}/{ref!r} carries {field!r} but no {_KEY_ID_FIELD!r}; half a credential signs nothing")
    pair[_KEY_ID_FIELD] = key_id
    return pair
