"""Ingest signs the lineage event it stages for recovery, as itself ([[LH-064]]).

A run reaches the lineage graph over HTTP, where the door authenticates the caller from its projected token and
records the caller as the author. When that door refuses, `ingest.lineage` stages the event in the object-store
outbox, and lineage's drain re-ingests it as it would a bus event, with no caller to authenticate: the author and the
signature have to be on the object. So the staged copy is stamped with this service's identity as its author and signed
as that identity, the one thing a verifier accepts without a delegation.

A SERVICE WITHOUT ITS KEY STAGES NOTHING. A verifier refuses an unsigned event and acknowledges the refusal, so a staged
copy it cannot verify would be deleted unread, and the recovery path would only look like one. The unresolved key is
reported (`/readyz`, and a log line naming the run), and the emitter has already counted the undelivered event.

The holder is a module slot rather than `app.state` because the stager runs inside workflow activities, which have no
request and no app to read it from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ingest.config import settings
from lineage_kit import AUTHOR_RUN_FACET, SigningKey, attach_signature, custom_facet, parse_published_keys
from service_kit.governed.signing_key import SigningKeyHolder, attach_signing, make_signing_holder


if TYPE_CHECKING:
    from fastapi import FastAPI


_holder: SigningKeyHolder[SigningKey] | None = None


async def start_signing(app: FastAPI) -> SigningKeyHolder[SigningKey] | None:
    """Build this service's key holder, publish it for the readiness check, and start resolving.

    Returns the holder for `stop_signing`, or None when the service does not sign.
    """
    global _holder
    config = settings()
    holder = make_signing_holder(
        secrets_from_dapr=config.secrets_from_dapr,
        identity=config.signing_identity,
        store=config.secret_store,
        load_key=SigningKey.from_seed,
        parse_published=parse_published_keys,
    )
    attach_signing(app, holder)
    _holder = holder
    if holder is not None:
        await holder.start()
    return holder


async def stop_signing(holder: SigningKeyHolder[SigningKey] | None) -> None:
    """Stop the holder's background resolution and uninstall it."""
    global _holder
    _holder = None
    if holder is not None:
        await holder.stop()


def signed_for_recovery(wire: dict[str, Any]) -> dict[str, Any]:
    """The event as lineage's drain will meet it: authored by this service and signed as it.

    A service that does not sign (a stack with no secret store) returns the event unchanged.

    Raises:
        SigningKeyUnavailableError: this service signs and has no key. Nothing may be staged.
    """
    holder = _holder
    if holder is None:
        return wire
    run = wire["run"]
    authored = {**wire, "run": {**run, "facets": {**run.get("facets", {}), AUTHOR_RUN_FACET: custom_facet(name=holder.identity, sub=holder.identity)}}}
    return attach_signature(authored, key=holder.key(), identity=holder.identity)
