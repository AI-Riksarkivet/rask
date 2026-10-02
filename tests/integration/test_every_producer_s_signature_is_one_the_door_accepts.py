"""The producers' real events and the one door agree, end to end, with nothing standing in for the door.

[[LH-064]]. Each producer is tested against the signing SEAM and the door is tested against its own fixtures, so both
halves can be green while they disagree about the document. They did: the door verified `event.model_dump(by_alias=True)`
while every producer signed the dict it publishes, and `RunEvent`'s facet fields are `Field(default_factory=dict)`, so the
dump grew bags the wire never carried and the signature failed for an honest producer. Nothing in either suite could see it.

THE PRODUCERS' REAL BUILDERS, NOT A HAND-WRITTEN EVENT. What each service actually publishes is what has to verify: the
medallion's `build_run_event` strips empty facet bags before it emits, the maintenance emitter builds its own shape, and the
catalog's stamps a PERSON as the author. A fixture that carried the same facets for all three would prove they agree with the
fixture. The signature is made by the root conftest's `EventSigner`, which writes the wire format from the contract alone, so
the door is checked for conformance and not against the code that signs.

THE DOOR'S REAL FUNCTION, driven as the bus drives it: `enforce_bus_authz` takes the parsed model AND the arrived mapping, and
the arrived mapping is the one the signature covers. Public keys are read where the door reads them, through the Dapr secret
API, stood in for by respx. FGA is off, so the signature is what answers.

THREE SHAPES, because they are genuinely different: self-signed (maintenance, the medallion), and a declared delegation (the
catalog, acting for the person it authenticated).
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import respx
from lance_namespace import PermissionDeniedError

from lineage.api import fga_deps
from lineage.core.config import LineageSettings, Signing
from lineage.models import UnverifiedEventError, parse_event
from service_kit.governed import fga


PERSON = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"
CATALOG = "service-catalog"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"
#: Fixed, so an event is the same document on every run: a signature over a generated id would still verify, and a failure
#: would be unreadable against a body that changed underneath it.
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
    """What `services/medallion` publishes, from its own builder: empty facet bags already stripped."""
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
    """What `services/catalog` publishes: authored by the signed-in PERSON, not by the service."""
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


#: `(name, builder, signer identity, on_behalf_of)`. The catalog is the only DELEGATED one, and that asymmetry is the point
#: rather than an inconsistency in the fixture.
PRODUCERS = [
    ("maintenance", _maintenance_event, "service-maintenance", None),
    ("medallion", _medallion_event, "service-bronze-to-silver", None),
    ("catalog", _catalog_event, CATALOG, PERSON),
]


def _settings(signers: set[str], delegators: set[str], *, fga_enabled: bool = False) -> LineageSettings:
    auth = {"oidc_enabled": True, "oidc_issuer": "https://dex.example", "oidc_audience": "lance", "fga_store_id": "s", "fga_model_id": "m"}
    return LineageSettings.model_validate(
        {"signing": Signing(signers=frozenset(signers), delegators=frozenset(delegators)), "fga_enabled": fga_enabled, **(auth if fga_enabled else {})}
    )


async def _drive(event: dict[str, Any], settings: LineageSettings) -> None:
    """Through the REAL door, as the bus drives it: the parsed model plus the bytes that arrived."""
    request = cast("Any", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fga=object()))))
    parsed = parse_event(json.loads(json.dumps(event)))
    await fga_deps.enforce_bus_authz(parsed, request, settings, event)


@respx.mock
@pytest.mark.parametrize(("name", "builder", "identity", "on_behalf_of"), [pytest.param(*p, id=p[0]) for p in PRODUCERS])
@pytest.mark.asyncio
async def test_the_door_admits_what_this_producer_signs(name: str, builder: Any, identity: str, on_behalf_of: str | None, event_signer: Any) -> None:
    """IF THIS IS RED the producer and the door disagree about the document, and every sweep, cascade hop or catalog write from
    that service is refused at the bus: an ack that discards the event, and on the outbox drain the deletion of its only copy."""
    signer = event_signer(identity)
    respx.get(f"{SECRETS}/signing-public-{identity}").mock(return_value=httpx.Response(200, json={"keys": signer.public}))
    event = signer.sign(builder(on_behalf_of or identity), on_behalf_of=on_behalf_of)

    await _drive(event, _settings({identity}, {CATALOG}))


@respx.mock
@pytest.mark.parametrize(("name", "builder", "identity", "on_behalf_of"), [pytest.param(*p, id=p[0]) for p in PRODUCERS if p[0] != "medallion"])
@pytest.mark.asyncio
async def test_a_peers_key_is_refused_for_the_same_event(name: str, builder: Any, identity: str, on_behalf_of: str | None, event_signer: Any) -> None:
    """The control, per producer. Without it every admission above is equally consistent with a door that admits everything."""
    published, peer = event_signer(identity), event_signer(identity)
    respx.get(f"{SECRETS}/signing-public-{identity}").mock(return_value=httpx.Response(200, json={"keys": published.public}))
    event = peer.sign(builder(on_behalf_of or identity), on_behalf_of=on_behalf_of)

    with pytest.raises(UnverifiedEventError) as refused:
        await _drive(event, _settings({identity}, {CATALOG}))

    assert refused.value.reason == "kid"


def _a_catalog_drop() -> dict[str, Any]:
    return {
        "eventTime": EVENT_TIME,
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "dataset": {"namespace": "acme", "name": "acme$events", "facets": {"lance": {"operation": "drop_table"}, "author": {"name": PERSON, "sub": PERSON}}},
    }


@pytest.mark.parametrize(
    ("sign", "event", "fga_enabled", "refused"),
    [
        pytest.param(False, _maintenance_event("service-maintenance"), False, False, id="an-unsigned-event-is-admitted"),
        pytest.param(True, _a_catalog_drop(), True, True, id="a-catalog-drop-nobody-verified-keeps-the-full-check"),
    ],
)
@pytest.mark.asyncio
async def test_an_estate_that_lists_no_signer_verifies_nothing_and_trusts_no_signature(
    sign: bool, event: dict[str, Any], fga_enabled: bool, refused: bool, event_signer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state the first deploy runs in: signing ships before it is enforced, so the door must admit what it admitted before and
    read no key, and a `rask_signature` it never verified must buy nothing. The second case is the attack: a drop that names the
    catalog gets no grant bypass from a signature that was merely present."""

    async def nobody_may_write(_client: object, *, objects: list[str], **_kw: object) -> dict[str, bool]:
        return dict.fromkeys(objects, False)

    async def no_tuples(_client: object, _obj: str) -> list[object]:
        return []

    monkeypatch.setattr(fga, "batch_check", nobody_may_write)
    monkeypatch.setattr(fga, "read_object_tuples", no_tuples)
    arriving = event_signer(CATALOG).sign(event, on_behalf_of=PERSON) if sign else event

    with pytest.raises(PermissionDeniedError) if refused else nullcontext() as outcome:
        await _drive(arriving, _settings(set(), set(), fga_enabled=fga_enabled))

    assert not isinstance(getattr(outcome, "value", None), UnverifiedEventError), "the refusal came from the signature gate, which this estate does not run"
