"""The one door every lineage event this service emits leaves by.

Nine call sites across four modules repeated the same eight-argument
:func:`service_kit.lakehouse.outbox.publish_lineage_with_outbox` call with ``json.dumps(event)``. That
was tolerable while the payload was the only thing that varied; it stopped being tolerable when the
events had to carry a producer SIGNATURE ([[LH-064]]), because signing at the emit means signing in
nine places and in every place added later — and a site that forgot would emit something
indistinguishable from a signed event until a reader checked.

ONE POINT, so the signature is a property of the service rather than of each caller.
``tests/test_the_cascade_signs_what_it_emits.py`` gates the other half: nothing else in this service
may call the outbox seam.

THE SIGNED BYTES ARE THE PUBLISHED BYTES AND THE STAGED BYTES. The event is serialised exactly once,
after signing, and that string is what the seam stages and publishes — and what the reconcile relay
later re-reads. A second serialisation anywhere in that chain would produce a different document from
the one the HMAC covers, which is the defect the bus door was carrying until this morning.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from lineage_kit.signing import attach_signature
from medallion.core.config import MedallionSettings, dedicated_token_for
from service_kit.governed.dapr_auth import SecretStoreUnreadable
from service_kit.lakehouse import outbox


log = logging.getLogger(__name__)


#: Resolved credentials, keyed on what determines them. A plain dict rather than `lru_cache` because
#: the input is the settings object and pydantic models are not hashable; the KEY is the triple that
#: decides the answer, so a reconfigured process gets a new credential rather than the first one
#: forever.
_KEYS: dict[tuple[str, str, bool], str] = {}


def reset_signing_key() -> None:
    """Drop the cached credential. For tests, and for a caller that has changed the configuration."""
    _KEYS.clear()


def signing_key(settings: MedallionSettings) -> str:
    """This service's OWN credential, or empty when there is none to be had.

    CACHED because the cascade emits on every hop of every run, and a secret-store round trip per event
    would put that store on the path of work that has already happened.

    EMPTY IS A REAL ANSWER, and it is the rollout: the bus door admits an unsigned event and refuses
    one that does not verify, so an estate that has not provisioned this identity keeps its cascade
    provenance instead of losing it. What this never does is substitute a placeholder — something that
    looks signed and verifies for nobody is strictly worse than nothing.
    """
    cache_key = (settings.fga_service_identity, settings.dapr_secret_store, settings.secrets_from_dapr)
    if cache_key not in _KEYS:
        _KEYS[cache_key] = _resolve_key(settings)
    return _KEYS[cache_key]


def _resolve_key(settings: MedallionSettings) -> str:
    resolver = dedicated_token_for(settings)
    if resolver is None:
        return ""
    try:
        return resolver(settings.fga_service_identity) or ""
    except SecretStoreUnreadable:
        # AN OUTAGE MUST NOT STOP A CASCADE. The door still admits unsigned events, so a store blip
        # degrades to today's behaviour; raising here would fail a stage for a signature it is not yet
        # required to produce.
        log.warning("medallion_signing_key_unreadable", extra={"identity": settings.fga_service_identity})
        return ""


async def emit_lineage(client: object, settings: MedallionSettings, event: dict[str, Any]) -> None:
    """Sign, stage, publish and drop one OpenLineage event.

    ``client`` is the Dapr client the caller already holds — the workflow activities build their own
    per-call, the request handlers reuse the one on the request, and neither concern belongs here.
    """
    key = signing_key(settings)
    if key:
        # THE SERVICE IDENTITY, never `settings.author`. The author facet carries both — `name` is the
        # role a person reads on a board, `sub` is what the bus door authorizes as — and
        # `verify_signed_event` requires the signer to equal the stamped `sub`, so signing as the role
        # would make the estate refuse its own cascade.
        event = attach_signature(event, key=key, identity=settings.fga_service_identity)
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
