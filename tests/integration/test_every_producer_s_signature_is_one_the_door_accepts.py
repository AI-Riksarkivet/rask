"""The three producers and the one door agree, end to end, with nothing standing in for either side.

[[LH-064]]. Each producer is tested against the signing SEAM and the door is tested against its own
fixtures, so both halves can be green while they disagree about the document. They did: the door
verified `event.model_dump(by_alias=True)` while every producer signed the dict it publishes, and
`RunEvent`'s facet fields are `Field(default_factory=dict)` — so the dump grew bags the wire never
carried and the HMAC failed for an honest producer. Nothing in either suite could see it.

THE PRODUCERS' REAL BUILDERS, NOT A HAND-WRITTEN EVENT. What each service actually publishes is what
has to verify: the medallion's `build_run_event` strips empty facet bags before it emits, the
maintenance emitter builds its own shape, and the catalog's stamps a PERSON as the author. A fixture
that carried the same facets for all three would prove they agree with the fixture.

THE DOOR'S REAL FUNCTION, driven as the bus drives it. `enforce_bus_authz` takes the parsed model AND
the arrived mapping, and the arrived mapping is the one the signature covers; the output-authz check
below it is a different question with its own suite, so it is stubbed and the signature gate is what
answers.

THREE SHAPES, because they are genuinely different and only one of them is the common case:
self-signed (maintenance, the medallion), and a declared delegation (the catalog, acting for the
person it authenticated).
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from lance_namespace import PermissionDeniedError

from lineage.api import fga_deps
from lineage.models import RunEvent
from lineage_kit.signing import attach_signature


KEY = "Hs2kQ9mW4xZ7vB1nC6jL0pR3tY5uI8oA2eD4fG6h"
PEER = "Tz8qW3eR6tY9uI1oP4aS7dF0gH2jK5lX8cV1bN4m"
PERSON = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"
#: Fixed, so an event is the same document on every run — a signature over a generated id would still
#: verify, and a failure would be unreadable against a body that changed underneath it.
RUN_ID = "0198e0f2-1b2c-7a3d-8e4f-5a6b7c8d9e0f"
EVENT_TIME = "2026-09-24T10:00:00Z"


def _maintenance_event(identity: str) -> dict[str, Any]:
    """What `services/maintenance` publishes, from its own builder."""
    from maintenance.core.lineage_emit import build_maintenance_event

    return build_maintenance_event(
        table_id="acme$events",
        namespace="acme",
        job_namespace="lance-maintenance",
        run_id=RUN_ID,
        event_time=EVENT_TIME,
        author=identity,
    )


def _medallion_event(identity: str) -> dict[str, Any]:
    """What `services/medallion` publishes, from its own builder — empty facet bags already stripped."""
    from medallion.schemas.events import build_run_event

    return build_run_event(
        operation="stage_silver",
        author="data_eng",
        author_subject=identity,
        job_namespace="lance-medallion",
        inputs=[("lance", "bronze$events")],
        output_namespace="lance",
        output_name="silver$events",
        token="a-token",
        event_type="COMPLETE",
    )


def _catalog_event(author: str) -> dict[str, Any]:
    """What `services/catalog` publishes — authored by the signed-in PERSON, not by the service."""
    from catalog.core.lineage_emit import build_write_event

    return build_write_event(
        table_id="acme$events",
        namespace="acme",
        author=author,
        version=3,
        operation="insert",
        run_id=RUN_ID,
        event_time=EVENT_TIME,
        job_namespace="lance-catalog",
    )


#: `(name, builder, signer identity, on_behalf_of)`. The catalog is the only DELEGATED one, and that
#: asymmetry is the point rather than an inconsistency in the fixture.
PRODUCERS = [
    ("maintenance", _maintenance_event, "service-maintenance", None),
    ("medallion", _medallion_event, "service-bronze-to-silver", None),
    ("catalog", _catalog_event, "service-catalog", PERSON),
]


def _signed(builder: Any, identity: str, on_behalf_of: str | None) -> dict[str, Any]:  # noqa: ANN401
    stamped = builder(on_behalf_of or identity)
    return attach_signature(stamped, key=KEY, identity=identity, on_behalf_of=on_behalf_of)


async def _drive(event: dict[str, Any], *, key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the REAL door, as the bus drives it: the parsed model plus the bytes that arrived."""

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(fga_deps, "dedicated_token_from_store", lambda _store: lambda _identity: key)
    monkeypatch.setattr(fga_deps, "enforce_output_authz", _allow)
    settings = cast("Any", type("S", (), {"fga_enabled": True, "dapr_secret_store": "lance-secrets"})())
    parsed = RunEvent.model_validate(json.loads(json.dumps(event)))
    await fga_deps.enforce_bus_authz(parsed, cast("Any", object()), settings, event)


@pytest.mark.parametrize(("name", "builder", "identity", "on_behalf_of"), PRODUCERS, ids=[p[0] for p in PRODUCERS])
@pytest.mark.asyncio
async def test_the_door_ADMITS_what_this_producer_signs(
    name: str, builder: Any, identity: str, on_behalf_of: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IF THIS IS RED the producer and the door disagree about the document, and every sweep, cascade
    hop or catalog write from that service is refused at the bus — a `_DROP` onto the dead-letter
    topic, and on the outbox drain the deletion of the event's only durable copy."""
    await _drive(_signed(builder, identity, on_behalf_of), key=KEY, monkeypatch=monkeypatch)


@pytest.mark.parametrize(("name", "builder", "identity", "on_behalf_of"), PRODUCERS, ids=[p[0] for p in PRODUCERS])
@pytest.mark.asyncio
async def test_a_PEER_key_is_refused_for_the_same_event(
    name: str, builder: Any, identity: str, on_behalf_of: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control, per producer. Without it every admission above is equally consistent with a door
    that admits everything, which is the door this replaced."""
    with pytest.raises(PermissionDeniedError, match="signature"):
        await _drive(_signed(builder, identity, on_behalf_of), key=PEER, monkeypatch=monkeypatch)


@pytest.mark.parametrize(("name", "builder", "identity", "on_behalf_of"), PRODUCERS, ids=[p[0] for p in PRODUCERS])
@pytest.mark.asyncio
async def test_an_UNSIGNED_event_from_the_same_builder_is_still_admitted(
    name: str, builder: Any, identity: str, on_behalf_of: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE ROLLOUT IS ORDER-INDEPENDENT, and this is the leg that says so.

    A new door meets old pods that do not sign yet, and staged outbox objects written before any of
    this drain through the same door later. Both are unsigned, and unsigned is admitted — so producers
    and the door may roll in either order. That property is what makes the deploy safe, and it is not
    obvious enough to leave to reasoning.
    """
    await _drive(builder(on_behalf_of or identity), key=KEY, monkeypatch=monkeypatch)
