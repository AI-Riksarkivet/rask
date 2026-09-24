"""Maintenance signs the lineage events it publishes, so the bus door can prove who emitted them.

[[LH-064]]. The bus door now refuses a signature that does not verify
(`lineage.api.fga_deps.enforce_signature_if_present`), and an unsigned event still passes — that is the
rollout. This is the first producer to stop relying on it.

ONE SIGNING POINT PER SERVICE, at `_publish`, which is the single place every maintenance lineage event
goes through. The alternative — signing in the pure builders — would need it in two of them today and
in every one added later, and a builder that forgot would emit an unsigned event that looks identical
to a signed one until someone checks.

THE IDENTITY IS THE ONE IT ALREADY STAMPS. `DaprMaintenanceEmitter` holds `author`, described in its
own docstring as "the service's OWN identity … the same string it presents at the catalog's service
door". Signing under that name is what makes the signature say something the graph can act on, and
`verify_signed_event` then requires it to equal the stamped `author.sub` — which it is, because the
same value produced both.

NO KEY MEANS NO SIGNATURE, never a fake one. An unconfigured deployment emits exactly what it emits
today; it does not emit something that looks signed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lineage_kit.signing import signature_of, verify_signed_event
from maintenance.core.lineage_emit import DaprMaintenanceEmitter


KEY = "AhaS3VaDMfKnpj82CKQTvmIGP200Nl85NKvug7Wm"
IDENT = "service-maintenance"


class _Captured:
    """A sidecar that records the payload instead of publishing it."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, *, data: str = "", **_kw: Any) -> None:
        self.published.append(json.loads(data))


async def _emit(*, signing_key: str) -> dict[str, Any]:
    client = _Captured()
    emitter = DaprMaintenanceEmitter(
        client,  # ty: ignore[invalid-argument-type]
        pubsub="p",
        topic="lineage.events.v1",
        job_namespace="lance",
        timeout_seconds=1.0,
        author=IDENT,
        signing_key=signing_key,
    )
    await emitter.emit_maintenance(table_id="acme-silver$dummy", namespace="lance")
    assert client.published, "nothing was published"
    return client.published[-1]


@pytest.mark.asyncio
async def test_a_signed_emit_VERIFIES_with_the_services_own_key() -> None:
    payload = await _emit(signing_key=KEY)
    found = signature_of(payload)
    assert found is not None, f"the event carries no signature: {sorted(payload)}"
    assert found.identity == IDENT, "the signature must name the identity the service presents elsewhere"
    assert verify_signed_event(payload, key=KEY), "the published signature does not verify with the key that made it"


@pytest.mark.asyncio
async def test_NO_KEY_emits_the_event_UNSIGNED_rather_than_faking_one() -> None:
    """The rollout property. An unconfigured deployment must not emit something that looks signed."""
    payload = await _emit(signing_key="")
    assert signature_of(payload) is None, "an unkeyed emitter attached a signature"


@pytest.mark.asyncio
async def test_a_PEER_key_does_not_verify_what_this_service_signed() -> None:
    """The point of the whole exercise: the signature distinguishes THIS producer from its neighbours."""
    payload = await _emit(signing_key=KEY)
    assert not verify_signed_event(payload, key="hBvaR8ijWCnpj82CKQTvmIGP200Nl85NKvug7WmF")
