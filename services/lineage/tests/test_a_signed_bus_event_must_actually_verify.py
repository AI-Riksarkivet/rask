"""A bus event that CARRIES a signature must produce one that checks out, or it is refused.

[[LH-064]]. The bus door authenticates the SIDECAR and reads the author off the payload, so a producer
holding the shared app token can record provenance as anyone. `lineage_kit.signing` closes that, and
this is the door half.

VERIFY-IF-PRESENT, AND THAT IS THE WHOLE ROLLOUT. An unsigned event is accepted exactly as today —
three producer paths still have to be taught to sign (the medallion's shared outbox seam, the catalog
and maintenance each publishing to their own sidecar), and refusing unsigned events before they do
would take the lineage bus down estate-wide. What changes is that a signature which does NOT verify is
now a refusal rather than a decoration. That is strictly stronger than today and breaks nothing:
nothing signs yet, so nothing can fail it.

IT IS ALSO THE ONLY WAY TO GATE THIS HOP BEFORE THE FLIP. A signer with a bug — wrong key, wrong
canonicalisation, signing a body that excludes the author — would otherwise be accepted silently right
up until the day unsigned events start being refused, which is the worst moment to discover it.

THE ABSENT-VS-UNREADABLE SPLIT RIDES ALONG, because it decides the status a caller gets. A signature
naming an identity with NO provisioned credential is a refusal on the merits (403). A secret store that
cannot be read is an outage (503) — collapsing them would 403 every honest producer during a store
blip and send an operator to the wrong system.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from lance_namespace import PermissionDeniedError, ServiceUnavailableError

from lineage.api import fga_deps
from lineage_kit.signing import attach_signature
from service_kit.governed.dapr_auth import SecretStoreUnreadable


KEY = "AhaS3VaDMfKnpj82CKQTvmIGP200Nl85NKvug7Wm"
OTHER = "hBvaR8ijWCnpj82CKQTvmIGP200Nl85NKvug7WmF"
IDENT = "service-bronze-to-silver"


def _payload(sub: str = IDENT) -> dict[str, Any]:
    return {
        "eventTime": "2026-09-24T00:00:00Z",
        "run": {"runId": "0198e0f2-1b2c-7a3d-8e4f-5a6b7c8d9e0f", "facets": {"author": {"sub": sub}}},
        "job": {"namespace": "lance", "name": "stage.silver"},
        "outputs": [{"namespace": "lance", "name": "silver$features"}],
    }


#: KEY-AWARE ON PURPOSE. A resolver that answers the same key whatever identity it is asked for cannot
#: see the door keying on `author.sub` instead of the signature's own `identity` — and that substitution
#: is the forgery this exists to stop. Measured: with an argument-blind double, mutating the source to
#: key on the author left every test green.
def keyed(**by_identity: str) -> Any:
    return lambda identity: by_identity.get(identity)


def _check(payload: dict[str, Any], resolver: Any) -> None:
    """Run ONLY the signature gate. The resolver arrives as a FACTORY, so an unsigned event never
    builds one — which is what keeps the secret store off the ingest path for every event today."""
    fga_deps.enforce_signature_if_present(payload, lambda: resolver)


def test_an_UNSIGNED_event_is_untouched() -> None:
    """The rollout property: nothing signs yet, so nothing may start failing."""
    _check(_payload(), keyed(**{IDENT: KEY}))


def test_a_VALID_signature_passes() -> None:
    _check(attach_signature(_payload(), key=KEY, identity=IDENT), keyed(**{IDENT: KEY}))


def test_a_signature_that_does_NOT_verify_is_REFUSED() -> None:
    """The row's own closing sentence, as a test."""
    signed = attach_signature(_payload(), key=KEY, identity=IDENT)
    with pytest.raises(PermissionDeniedError, match="signature"):
        _check(signed, keyed(**{IDENT: OTHER}))


def test_a_signer_claiming_ANOTHER_subject_is_REFUSED() -> None:
    """Covering the author is not enough — the signer must BE the author (`verify_signed_event`)."""
    forged = attach_signature(_payload(sub="service-trainer"), key=KEY, identity=IDENT)
    with pytest.raises(PermissionDeniedError, match="signature"):
        _check(forged, keyed(**{IDENT: KEY, "service-trainer": OTHER}))


def test_an_identity_with_NO_credential_is_a_REFUSAL_not_a_crash() -> None:
    """`None` from the resolver means "read the store, this subject has none" — 403 on the merits."""
    signed = attach_signature(_payload(), key=KEY, identity=IDENT)
    with pytest.raises(PermissionDeniedError, match="signature"):
        _check(signed, keyed())


def test_an_UNREADABLE_store_is_an_OUTAGE_not_a_refusal() -> None:
    """503, never 403. A store blip must not read as "your signature is bad" on every honest producer."""

    def _down(_identity: str) -> str | None:
        raise SecretStoreUnreadable("secret store unreadable")

    signed = attach_signature(_payload(), key=KEY, identity=IDENT)
    with pytest.raises(ServiceUnavailableError):
        _check(signed, _down)


# --------------------------------------------------------------------------- #
# THE WIRING HOP. Everything above tests the gate; none of it notices the DOOR not calling it.
# Measured: deleting the call from `enforce_bus_authz` left all six green.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_BUS_DOOR_actually_runs_the_signature_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A signature that does not verify must be refused by `enforce_bus_authz`, not merely by the helper.

    IF THIS IS RED the gate exists and nothing calls it — the shape "a gate on the innermost call
    proves nothing". It is driven with a resolver that hands back the WRONG key, so the refusal can
    only come from the signature check and not from the output authz below it.
    """
    from lineage.models import RunEvent

    monkeypatch.setattr(fga_deps, "dedicated_token_from_store", lambda _store: keyed(**{IDENT: OTHER}))
    settings = SimpleNamespace(fga_enabled=True, dapr_secret_store="lance-secrets")
    event = RunEvent.model_validate({**attach_signature(_payload(), key=KEY, identity=IDENT), "eventType": "COMPLETE"})

    # CAST, never an ignore: the door takes a Request and a LineageSettings and this supplies neither,
    # because the signature gate refuses before either is read. Saying so in a cast keeps the claim
    # checkable — an ignore would hide it if the door later started reading them first.
    from fastapi import Request

    from lineage.core.config import LineageSettings

    with pytest.raises(PermissionDeniedError, match="signature"):
        await fga_deps.enforce_bus_authz(
            event, cast("Request", SimpleNamespace()), cast("LineageSettings", settings), attach_signature(_payload(), key=KEY, identity=IDENT)
        )


@pytest.mark.asyncio
async def test_the_BUS_DOOR_ADMITS_a_signature_that_IS_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mirror of the test above, and the one that catches a door verifying the WRONG BYTES.

    Every other case here is driven with a key that cannot verify, so it passes whether the door
    checks the arrived event or some re-serialisation of it. Reproduced against the real models:
    `RunEvent`'s facet fields are `Field(default_factory=dict)`, so a `model_dump` materialises
    `job.facets = {}` and `outputs[0].facets = {}` that the producer never sent and never signed. The
    HMAC is over the canonical body, so the honest producer is refused — and on the bus that is a
    `_DROP` onto a `deadLetterTopic`, while on the outbox drain the staged copy is deleted outright.

    A signature covers THE BYTES THAT WERE SIGNED. Anything reconstructed from them is a different
    document, however faithfully it is rebuilt.
    """
    from lineage.models import RunEvent

    reached: list[str] = []

    async def _record(*_a: Any, **_k: Any) -> None:
        reached.append("output-authz")

    monkeypatch.setattr(fga_deps, "dedicated_token_from_store", lambda _store: keyed(**{IDENT: KEY}))
    monkeypatch.setattr(fga_deps, "enforce_output_authz", _record)
    settings = SimpleNamespace(fga_enabled=True, dapr_secret_store="lance-secrets")
    arrived = attach_signature(_payload(), key=KEY, identity=IDENT)
    event = RunEvent.model_validate({**arrived, "eventType": "COMPLETE"})

    from fastapi import Request

    from lineage.core.config import LineageSettings

    await fga_deps.enforce_bus_authz(event, cast("Request", SimpleNamespace()), cast("LineageSettings", settings), arrived)
    assert reached == ["output-authz"], "the signature gate refused an honest producer, or the door never reached the output check"


@pytest.mark.asyncio
async def test_a_BROKEN_signature_is_refused_even_when_FGA_IS_OFF(monkeypatch: pytest.MonkeyPatch) -> None:
    """A signature is AUTHENTICATION and must not be switchable off by an AUTHORIZATION toggle.

    `fga_enabled` decides whether the estate consults OpenFGA about what a subject may write. It says
    nothing about whether a producer is who it claims to be, and an estate running with auth on and FGA
    off would otherwise verify nothing at all — the door's strongest guarantee disabled by a flag
    describing a different system.
    """
    from lineage.models import RunEvent

    monkeypatch.setattr(fga_deps, "dedicated_token_from_store", lambda _store: keyed(**{IDENT: OTHER}))
    settings = SimpleNamespace(fga_enabled=False, dapr_secret_store="lance-secrets")
    arrived = attach_signature(_payload(), key=KEY, identity=IDENT)
    event = RunEvent.model_validate({**arrived, "eventType": "COMPLETE"})

    from fastapi import Request

    from lineage.core.config import LineageSettings

    with pytest.raises(PermissionDeniedError, match="signature"):
        await fga_deps.enforce_bus_authz(event, cast("Request", SimpleNamespace()), cast("LineageSettings", settings), arrived)
