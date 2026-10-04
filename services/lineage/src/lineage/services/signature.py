"""Verifying the signature an event carries against the public keys its signer publishes ([[LH-064]]).

The bus door authenticates the sidecar that delivered an event, never the producer that wrote it, so the author
stamped inside is a claim until a signature proves it. A producer signs with an Ed25519 key only its own sidecar can
read; lineage holds PUBLIC keys only, so a compromised lineage can admit or refuse events and cannot forge one.

WHAT THIS MODULE OWNS is the part the kit cannot: this pod's Dapr sidecar as the place each identity's published list is
read from (`lineage_kit.keys.PublishedKeys` caches and paces the reads, through service-kit's `published_key_fetch`, the
read every verifying door shares), and turning the kit's outcomes into the three answers a door needs.
`lineage_kit.signing.verify_signature` owns what a valid signature is.

* VERIFIED: the event's signer and the key that signed it are known. The caller may act on what the signature attests.
* REFUSED (`UnverifiedEventError`, a `PermissionDeniedError`): no listed signer's published key verifies the event.
  Final, so every door answers it without a retry.
* OUTAGE (`ServiceUnavailableError`): the list could not be read, or lacks the event's key id and no read has begun
  since that key id was first met, so the key that signed may have been published after the read that missed it. Says
  nothing about the signature, so the delivery is retried and a staged object stays staged. Refusing on an outage
  would destroy honest events during a store blip.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, Final

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ServiceUnavailableError

from lineage.core.config import LineageSettings
from lineage.core.metrics import record_signature_refused, record_signature_verified
from lineage.models import UnverifiedEventError, run_id_from_payload
from lineage_kit.keys import PublishedKeys
from lineage_kit.signing import KeySourceUnavailableError, SignatureError, VerifiedSignature, signature_of, verify_signature
from service_kit.governed.signing_key import published_key_fetch


log = logging.getLogger(__name__)

#: Bounds what an event's own text can put into a log line or a recorded refusal: the kit's messages quote the
#: identity and algorithm the event claims.
_MAX_REASON_CHARS: Final = 300


def dapr_published_keys(store: str, *, clock: Callable[[], float] = time.monotonic) -> PublishedKeys:
    """The kit's key reader, reading each identity's list through THIS pod's Dapr sidecar from secret store ``store``."""
    return PublishedKeys(published_key_fetch(store), clock=clock)


def _published_keys(request: Request, settings: LineageSettings) -> PublishedKeys:
    """This process's key reader, built on first use and kept on the app so its cache outlives one delivery."""
    state = request.app.state
    keys: PublishedKeys | None = getattr(state, "published_keys", None)
    if keys is None:
        keys = state.published_keys = dapr_published_keys(settings.dapr_secret_store)
    return keys


def _bounded(text: str) -> str:
    return text if len(text) <= _MAX_REASON_CHARS else f"{text[:_MAX_REASON_CHARS]}..."


def _subject_of(arrived: Mapping[str, Any]) -> dict[str, str | None]:
    """Which event a log line is about: its run, or the dataset a static change names. Only strings, and bounded."""
    dataset = arrived.get("dataset")
    name = dataset.get("name") if isinstance(dataset, dict) else None
    run_id = run_id_from_payload(arrived)
    return {"run_id": _bounded(run_id) if run_id else None, "dataset": _bounded(name) if isinstance(name, str) else None}


async def verify_arrived_event(arrived: Mapping[str, Any], request: Request, settings: LineageSettings) -> VerifiedSignature | None:
    """Verify the signature on the event exactly as it arrived, or answer ``None`` when this estate enforces none.

    ``None`` is the unenforced estate (`settings.signing` lists no signer): nothing is verified, a `rask_signature`
    the event carries is ignored, and nothing may be admitted on one. A caller reads a signature's meaning from
    the returned `VerifiedSignature`, never from the event, so it acts on what THIS call established.

    OVER THE BYTES THAT ARRIVED, never a re-serialisation: the producer signed the document it wrote, and a model
    dump grows the fields the schema defaults. Verification runs in the threadpool because it reads the store
    through a blocking client.

    Raises:
        UnverifiedEventError: no listed signer's published key verifies the event. Final.
        ServiceUnavailableError: the identity's published keys could not be read, or no read has begun since an
            unknown key id was first met and the last one is too recent to repeat. Retry.
    """
    signing = settings.signing
    if not signing.enforced:
        return None
    keys = _published_keys(request, settings)
    claim = signature_of(arrived)
    try:
        verified = await run_in_threadpool(
            verify_signature, arrived, source=keys.for_event(claim.kid if claim else None), signers=signing.signers, delegators=signing.delegators
        )
    except SignatureError as exc:
        record_signature_refused(exc.reason)
        log.warning(
            "lineage_signature_refused",
            extra={**_subject_of(arrived), "reason": exc.reason, "identity": _bounded(claim.identity) if claim else None},
        )
        raise UnverifiedEventError(_bounded(str(exc)), exc.reason) from exc
    except KeySourceUnavailableError as exc:
        log.warning("lineage_signature_keys_unavailable", extra={**_subject_of(arrived), "error": _bounded(str(exc))})
        raise ServiceUnavailableError(_bounded(str(exc))) from exc
    record_signature_verified(verified.identity)
    return verified
