"""Verifying the signature an event carries against the public keys its signer publishes ([[LH-064]]).

The bus door authenticates the transport an event arrived on, never the producer that wrote it, so the author
stamped inside is a claim until a signature proves it. A producer signs with an Ed25519 key only its own store role can
read; lineage holds PUBLIC keys only, so a compromised lineage can admit or refuse events and cannot forge one.

WHAT THIS MODULE OWNS is the part the library leaves to each service: the secret store, read under this service's own
login, as the place each identity's published list is read from (`lakehouse.events.published_keys.PublishedKeys` caches and paces the reads, through
`lakehouse.governed.signing_key.published_key_fetch`, the read every verifying door shares), and turning the library's
verdict (`lakehouse.events.door_verdict.judge`, enforcing: the one place a refusal is told from an outage) into the
three answers a door needs.
`lakehouse.events.signing.verify_signature` owns what a valid signature is.

* VERIFIED: the event's signer and the key that signed it are known. The caller may act on what the signature attests.
* REFUSED (`UnverifiedEventError`, a `PermissionDeniedError`): no listed signer's published key verifies the event.
  Final, so every door answers it without a retry.
* OUTAGE (`ServiceUnavailableError`): the list could not be read, or lacks the event's key id and no read has begun
  since that key id was first met, so the key that signed may have been published after the read that missed it. Says
  nothing about the signature, so the lane asks for the delivery again and a staged object stays staged. Refusing on an outage
  would destroy honest events during a store blip.
"""

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, Final

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ServiceUnavailableError

from lakehouse.events.door_verdict import DoorMode, DoorVerdict, judge
from lakehouse.events.published_keys import PublishedKeys
from lakehouse.events.signing import VerifiedSignature, signature_of, verify_signature
from lakehouse.governed.secrets import SecretStore
from lakehouse.governed.signing_key import published_key_fetch
from lineage.core.config import LineageSettings
from lineage.core.metrics import record_signature_refused, record_signature_verified
from lineage.core.refusals import UnverifiedEventError
from lineage.models import run_id_from_payload


log = logging.getLogger(__name__)

#: Bounds what an event's own text can put into a log line or a recorded refusal: the kit's messages quote the
#: identity and algorithm the event claims.
_MAX_REASON_CHARS: Final = 300


def store_published_keys(secrets: SecretStore | None, *, clock: Callable[[], float] = time.monotonic) -> PublishedKeys:
    """The kit's key reader, reading each identity's list from ``secrets``, this start's session with the secret store."""
    return PublishedKeys(published_key_fetch(secrets), clock=clock)


def _bounded(text: str) -> str:
    return text if len(text) <= _MAX_REASON_CHARS else f"{text[:_MAX_REASON_CHARS]}..."


def _subject_of(arrived: Mapping[str, Any]) -> dict[str, str | None]:
    """Which event a log line is about: its run, or the dataset a static change names. Only strings, and bounded."""
    dataset = arrived.get("dataset")
    name = dataset.get("name") if isinstance(dataset, dict) else None
    run_id = run_id_from_payload(arrived)
    return {"run_id": _bounded(run_id) if run_id else None, "dataset": _bounded(name) if isinstance(name, str) else None}


async def verify_arrived_event(arrived: Mapping[str, Any], app: FastAPI, settings: LineageSettings) -> VerifiedSignature | None:
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
    if not settings.signing.enforced:
        return None
    claim = signature_of(arrived)
    verdict = await _judged(arrived, app, settings)
    detail = _bounded(verdict.detail or "")
    if verdict.refused is not None:
        record_signature_refused(verdict.refused)
        log.warning(
            "lineage_signature_refused",
            extra={**_subject_of(arrived), "reason": verdict.refused, "identity": _bounded(claim.identity) if claim else None},
        )
        raise UnverifiedEventError(detail, verdict.refused)
    if verdict.verified is None:
        log.warning("lineage_signature_keys_unavailable", extra={**_subject_of(arrived), "error": detail})
        raise ServiceUnavailableError(detail)
    record_signature_verified(verdict.verified.identity)
    return verdict.verified


async def _judged(arrived: Mapping[str, Any], app: FastAPI, settings: LineageSettings) -> DoorVerdict:
    """The enforcing verdict on the signature ``arrived`` carries, against the published keys the lifespan's one reader
    holds (its cache and its pacing are the process's, shared by every door that verifies) and the chart's signer sets.
    In the threadpool: a key read is a blocking call."""
    keys: PublishedKeys = app.state.published_keys
    claim = signature_of(arrived)
    signing = settings.signing

    def verify() -> VerifiedSignature:
        return verify_signature(
            arrived, source=keys.for_event(claim.kid if claim else None), signers=signing.event_signers, delegators=signing.event_delegators
        )

    return await run_in_threadpool(judge, verify, DoorMode.ENFORCE)


async def staged_signature(arrived: Mapping[str, Any], app: FastAPI, settings: LineageSettings) -> VerifiedSignature | None:
    """The signature a staged event carries, verified over the same bytes, keys and signer sets as
    :func:`verify_arrived_event`, or ``None`` where that would not answer VERIFIED: signing not enforced, no signature, a
    refusal, or an outage.

    For a door that only READS a staged event, scoping what it names for its caller: it refuses and records nothing, so a
    failure to verify is no refusal to count or warn about, and the event takes the full check rather than the page
    failing. A door that records the event verifies with :func:`verify_arrived_event`, which answers each outcome.
    """
    if not settings.signing.enforced or signature_of(arrived) is None:
        return None
    return (await _judged(arrived, app, settings)).verified
