"""The one door every lineage event this service emits leaves by, and the one place it is signed ([[LH-064]]).

Every site that emits, across four modules, reaches the outbox through :func:`emit_lineage`, so the signature is a
property of the service rather than of each caller: a site that forgot would emit something indistinguishable from a
signed event until a reader checked. The import-linter contract
`the-cascade-publishes-lineage-through-the-signing-door` keeps the outbox seam reachable from this module alone.

THE EVENT IS SIGNED BEFORE IT IS STAGED, and the staged copy is the signed copy. The signature covers the canonical form
of the parsed event, so the text this module serialises is free to differ from what a hop re-encodes (the sidecar's
delivery, the relay's republish): every copy verifies, and the relay re-ingests exactly what was signed.

A SIGNER WITHOUT ITS KEY EMITS NOTHING. A verifier refuses an unsigned event and acknowledges the refusal, so the event
would be gone, and the cascade head reacts to the bronze-write event, so a run whose first event is lost never starts.
:func:`emit_lineage` therefore raises `SigningKeyUnavailableError` rather than send an unsigned event, and every caller
that already treated a failed emit as a retry (the COMPLETE of a stage, the bronze write) keeps doing so. The
sidecar-delivered routes answer RETRY before any of that while the key is unresolved (`retry_until_signed`), so the
raise is the backstop for a key lost mid-delivery.

SIGNING IS CONFIGURED BY THE SETTINGS, not by whether a holder happens to be installed: a service that is meant to sign
and has none installed raises too, so a path that never ran the lifespan cannot emit unsigned by accident.

THE CONTROL EVENTS THIS SERVICE EMITS ARE SIGNED HERE TOO ([[XC-078]]), by :func:`signed_control_event` with the same key
and the same rule: the producer's `promotion_review_requested`, which a door verifies against the producer's identity.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from lineage_kit import SigningKey, attach_control_signature, attach_signature, parse_published_keys
from medallion.core.config import MedallionSettings
from service_kit.governed.signing_key import SigningKeyHolder, SigningKeyUnavailableError, attach_signing, make_signing_holder
from service_kit.lakehouse import outbox


if TYPE_CHECKING:
    from fastapi import FastAPI


#: This process's key holder, installed by the lifespan. A module slot rather than `app.state` because the cascade
#: also emits from workflow activities, which have no request and no app to read it from.
_signing: SigningKeyHolder[SigningKey] | None = None


def signing_configured(settings: MedallionSettings) -> bool:
    """Whether this service is meant to sign: its secret store is wired and the chart named its signing identity."""
    return settings.secrets_from_dapr and bool(settings.signing_identity)


async def start_signing(app: FastAPI, settings: MedallionSettings) -> SigningKeyHolder[SigningKey] | None:
    """Build this service's key holder, publish it for the gate and the readiness check, and start resolving.

    Returns the holder for `stop_signing`, or None when the service does not sign.

    Raises:
        ValueError: the signing identity is not the subject the events stamp as their author, which a verifier would
            refuse for every event.
    """
    global _signing
    holder = make_signing_holder(
        secrets_from_dapr=settings.secrets_from_dapr,
        identity=settings.signing_identity,
        store=settings.dapr_secret_store,
        load_key=SigningKey.from_seed,
        parse_published=parse_published_keys,
    )
    if holder is not None and holder.identity != settings.fga_service_identity:
        raise ValueError(
            f"this service signs as {holder.identity!r} but stamps {settings.fga_service_identity!r} as its author: "
            "a verifier refuses a signer that is not its event's author, so every event would be lost"
        )
    attach_signing(app, holder)
    _signing = holder
    if holder is not None:
        await holder.start()
    return holder


async def stop_signing(holder: SigningKeyHolder[SigningKey] | None) -> None:
    """Stop the holder's background resolution and uninstall it."""
    global _signing
    _signing = None
    if holder is not None:
        await holder.stop()


def _holder(settings: MedallionSettings) -> SigningKeyHolder[SigningKey] | None:
    """The holder to sign with, or None when this service does not sign.

    Raises:
        SigningKeyUnavailableError: this service is meant to sign and no holder is installed.
    """
    holder = _signing
    if holder is None and signing_configured(settings):
        raise SigningKeyUnavailableError(f"{settings.signing_identity} is configured to sign but no key holder is installed")
    return holder


def require_signing_key(settings: MedallionSettings) -> None:
    """Raise unless this service can sign right now: it does not sign, or its holder holds a key.

    For an emit that must not count or announce what it then cannot send: the check comes first, and the emit signs with
    the same holder.

    Raises:
        SigningKeyUnavailableError: this service signs and holds no key.
    """
    holder = _holder(settings)
    if holder is not None:
        holder.key()


def _signed(settings: MedallionSettings, event: dict[str, Any]) -> dict[str, Any]:
    """The event as it leaves: signed as this service's identity, or unchanged when this service does not sign.

    THE SERVICE IDENTITY, never `settings.author`. The author facet carries both: `name` is the role a person reads on
    a board and `sub` is what the lineage door authorizes as, and a verifier requires the signer to be the stamped
    `sub`, so signing as the role would make the estate refuse its own cascade.
    """
    holder = _holder(settings)
    if holder is None:
        return event
    return attach_signature(event, key=holder.key(), identity=holder.identity)


def signed_control_event(settings: MedallionSettings, envelope: dict[str, Any]) -> dict[str, Any]:
    """A control event as it leaves: signed as this service's identity, or unchanged when this service does not sign.

    ``envelope`` is the JSON the bus carries (`CatalogControlEvent.model_dump_json()` read back), the document a door
    verifies. This service emits for itself, never for a person it authenticated, so the signature declares no
    delegation, and an envelope naming a person as its actor is refused by `attach_control_signature` here rather
    than by a door after it left.

    Raises:
        SigningKeyUnavailableError: this service signs and has no key to sign with. Nothing may then be sent.
    """
    holder = _holder(settings)
    if holder is None:
        return envelope
    return attach_control_signature(envelope, key=holder.key(), identity=holder.identity)


async def emit_lineage(client: object, settings: MedallionSettings, event: dict[str, Any]) -> None:
    """Sign, stage, publish and drop one OpenLineage event.

    ``client`` is the Dapr client the caller already holds — the workflow activities build their own
    per-call, the request handlers reuse the one on the request, and neither concern belongs here.

    Raises:
        SigningKeyUnavailableError: this service signs and has no key to sign with. Nothing is staged or published.
    """
    event = _signed(settings, event)
    await outbox.publish_lineage_with_outbox(
        client,
        outbox_uri=settings.lineage_outbox_uri,
        storage_options=settings.storage_options(),
        run_id=str(event["run"]["runId"]),
        event_json=json.dumps(event),
        pubsub_name=settings.pubsub,
        topic_name=settings.lineage_topic,
        timeout_seconds=settings.publish_timeout_seconds,
    )
