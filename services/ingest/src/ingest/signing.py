"""Ingest signs the lineage event it stages for recovery, as itself ([[LH-064]]).

A run reaches the lineage graph over HTTP, where the door authenticates the caller from its projected token and
records the caller as the author. When that door refuses, `ingest.lineage` stages the event in the object-store
outbox, and lineage's drain re-ingests it as it would a bus event, with no caller to authenticate: the author and the
signature have to be on the object. So the staged copy is stamped with this service's identity as its author and signed
as that identity, the one thing a verifier accepts without a delegation.

A SERVICE WITHOUT ITS KEY STAGES NOTHING. A verifier refuses an unsigned event and acknowledges the refusal, so a staged
copy it cannot verify would be deleted unread, and the recovery path would only look like one. The unresolved key is
reported (`/readyz`, and a log line naming the run), and the emitter has already counted the undelivered event.

A SERVICE THAT HAS NOT SAID WHETHER IT SIGNS STAGES NOTHING EITHER. The slot holds one of three answers: a holder (this
service signs), `DOES_NOT_SIGN` (`make_signing_holder` returned no holder: no secret store, or no identity) and
`NOT_INSTALLED` (no answer yet, or withdrawn). The third is not the second: a service that signs reads as
`NOT_INSTALLED` until its lifespan has installed the holder, and an event staged then would carry no author and no
signature. The lifespan installs the answer before the workflow worker starts and withdraws it after the worker has
stopped, so a recovered activity always meets one; refusing on `NOT_INSTALLED` keeps a caller outside that order from
staging a copy the drain would delete.

The slot is a module slot rather than `app.state` because the stager runs inside workflow activities, which have no
request and no app to read it from.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from ingest.config import settings
from lineage_kit import AUTHOR_RUN_FACET, SigningKey, attach_signature, custom_facet, parse_published_keys
from service_kit.governed.signing_key import SigningKeyHolder, SigningKeyUnavailableError, attach_signing, make_signing_holder


if TYPE_CHECKING:
    from fastapi import FastAPI


class _NoHolder(Enum):
    """Why the slot holds no key holder."""

    NOT_INSTALLED = auto()
    DOES_NOT_SIGN = auto()


_slot: SigningKeyHolder[SigningKey] | _NoHolder = _NoHolder.NOT_INSTALLED


async def start_signing(app: FastAPI) -> SigningKeyHolder[SigningKey] | None:
    """Build this service's key holder, publish it for the readiness check, and start resolving.

    Returns the holder for `stop_signing`, or None when the service does not sign.
    """
    global _slot
    config = settings()
    holder = make_signing_holder(
        secrets_from_dapr=config.secrets_from_dapr,
        identity=config.signing_identity,
        store=config.secret_store,
        load_key=SigningKey.from_seed,
        parse_published=parse_published_keys,
    )
    attach_signing(app, holder)
    _slot = _NoHolder.DOES_NOT_SIGN if holder is None else holder
    if holder is not None:
        await holder.start()
    return holder


async def stop_signing(holder: SigningKeyHolder[SigningKey] | None) -> None:
    """Stop the holder's background resolution and withdraw the answer."""
    global _slot
    _slot = _NoHolder.NOT_INSTALLED
    if holder is not None:
        await holder.stop()


def signed_for_recovery(wire: dict[str, Any]) -> dict[str, Any]:
    """The event as lineage's drain will meet it: authored by this service and signed as it.

    A service that does not sign (a stack with no secret store, or no identity) returns the event unchanged.

    Raises:
        SigningKeyUnavailableError: this service signs and has no key, or has not installed its holder. Nothing may be staged.
    """
    slot = _slot
    if slot is _NoHolder.NOT_INSTALLED:
        raise SigningKeyUnavailableError("the signing key holder is not installed: the lifespan has not started it, or has already stopped it")
    if slot is _NoHolder.DOES_NOT_SIGN:
        return wire
    run = wire["run"]
    authored = {**wire, "run": {**run, "facets": {**run.get("facets", {}), AUTHOR_RUN_FACET: custom_facet(name=slot.identity, sub=slot.identity)}}}
    return attach_signature(authored, key=slot.key(), identity=slot.identity)
