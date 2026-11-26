"""With signing enforced, lineage records a bus event only when a listed signer's key verifies it.

[[LH-064]] C1 (owner ruling 2026-10-02: Ed25519, keys in the store). The bus authenticates the connection that
published, not the producer, so a publisher the bus admits could otherwise record provenance as anyone. Enforced, an
unsigned event and one whose signature does not verify are refused: taken, so the bus neither delivers them again nor
has them parked, and not recorded. A public-key source lineage cannot read is an outage, so the delivery is asked for
again rather than refused. Each refusal is counted once under its reason and each verified event once under its signer.

Driven through lineage's own app and lifespan, each event delivered to its ingest lane, FGA off so only the signature
decides. The signer's public keys are read from the secret store, which the suite's double answers; the signatures are
built by the root conftest's `EventSigner` from the wire format alone. Every reason series of the refusal counter exists
at 0 from the moment the lane starts, so the alert's `rate()` sees the first refusal as an increase; the counter is read
through a real in-memory reader.
"""

import asyncio
import json
from collections.abc import Callable, Iterator
from typing import Any, get_args

import httpx
import pytest
from fastapi import FastAPI
from lineage_lane import Feed, Lineage, measured, started

from lakehouse.events.bus_delivery import Outcome, Settlement
from lakehouse.events.signing import RefusalReason
from lineage import main
from lineage.core.config import LineageSettings, get_settings
from tests.secret_store_double import SecretStoreDouble


SIGNER = "service-pipeline-a"


@pytest.fixture
def signature_counts(monkeypatch: pytest.MonkeyPatch) -> Callable[[], dict[tuple[str, str], int]]:
    """Both signature counters read through a real in-memory reader: ``("refused", reason)`` and ``("verified", identity)``."""
    counts = measured(monkeypatch, "_signature_refused", "_signature_verified")

    def by_series() -> dict[tuple[str, str], int]:
        refused = {("refused", dict(attributes)["lance.lineage.reason"]): count for attributes, count in counts("lineage.signature.refused").items()}
        verified = {("verified", dict(attributes)["lance.lineage.identity"]): count for attributes, count in counts("lineage.signature.verified").items()}
        return refused | verified

    return by_series


def _event() -> dict[str, Any]:
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-10-02T12:00:00+00:00",
        "run": {"runId": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", "facets": {"rask_author": {"name": SIGNER, "sub": SIGNER}}},
        "job": {"namespace": "bus", "name": "stage.silver"},
        "outputs": [{"namespace": "silver", "name": "acme-silver$events"}],
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
    }


@pytest.fixture
def lane(monkeypatch: pytest.MonkeyPatch, signature_counts: Callable[[], dict[tuple[str, str], int]], secret_store: SecretStoreDouble) -> Iterator[Lineage]:
    """Lineage started after `signature_counts`, as a pod starts its lane after its MeterProvider is installed."""
    del signature_counts, secret_store
    monkeypatch.setenv("RASK_EVENT_SIGNERS", json.dumps([SIGNER, "service-catalog"]))
    monkeypatch.setenv("RASK_EVENT_DELEGATORS", json.dumps(["service-catalog"]))
    with started(monkeypatch, Feed()) as lineage:
        yield lineage


@pytest.mark.parametrize(
    ("case", "again", "recorded", "counted"),
    [
        pytest.param("signed", False, True, ("verified", SIGNER), id="an-event-its-listed-signer-signed"),
        pytest.param("unsigned", False, False, ("refused", "unsigned"), id="an-unsigned-event"),
        pytest.param("tampered", False, False, ("refused", "signature"), id="an-event-changed-after-its-signer-signed-it"),
        pytest.param("store-down", True, False, None, id="a-public-key-source-lineage-cannot-read"),
    ],
)
def test_lineage_records_a_bus_event_only_when_a_listed_signer_signed_it(
    lane: Lineage,
    signature_counts: Callable[[], dict[tuple[str, str], int]],
    event_signer: Any,
    secret_store: SecretStoreDouble,
    respx_allows_unused_routes: None,
    case: str,
    again: bool,
    recorded: bool,
    counted: tuple[str, str] | None,
) -> None:
    signer = event_signer(SIGNER)
    secret_store.hold(f"signing-public-{SIGNER}", httpx.Response(500) if case == "store-down" else secret_store.held({"keys": signer.public}))
    event = _event() if case == "unsigned" else signer.sign(_event())
    if case == "tampered":
        event["outputs"] = [{"namespace": "gold", "name": "acme-gold$events"}]
    assert signature_counts() == {("refused", reason): 0 for reason in get_args(RefusalReason.__value__)}, (
        "starting the lane must create every refusal series at 0, or a first refusal is no increase"
    )

    settled = lane.deliver({"data": event})

    expected = Settlement(outcome=Outcome.AGAIN, delay_seconds=get_settings().delivery_retry_seconds) if again else Settlement(outcome=Outcome.TAKEN)
    assert settled == expected, f"{case}: the lane settled {settled}"
    assert bool(lane.feed.recorded) is recorded, f"{case}: recorded {lane.feed.recorded}"
    # A refusal is taken off the bus, so its count is the only trace it leaves; an outage is asked for again and counts nothing.
    assert {series: count for series, count in signature_counts().items() if count} == ({counted: 1} if counted else {})


def test_lineage_does_not_start_authenticated_with_a_secret_store_and_no_signer(monkeypatch: pytest.MonkeyPatch, secret_store: SecretStoreDouble) -> None:
    """A deployment with authentication and a secret store has one state: lineage verifies what its bus and outbox
    doors record. Listing no signer there would record a `rask_signature` nobody verified, so the start ends before
    it logs in to the store or serves."""
    authenticated = {"oidc_enabled": True, "RASK_OIDC_IDP_ID": "dex", "RASK_OIDC_ISSUER": "https://auth.example.com/dex", "RASK_OIDC_AUDIENCE": "rask"}
    monkeypatch.setattr(main, "get_settings", lambda: LineageSettings.model_validate(authenticated))

    with pytest.raises(RuntimeError, match="RASK_EVENT_SIGNERS lists no signer"):
        asyncio.run(main.lifespan(FastAPI()).__aenter__())

    assert secret_store.login.call_count == 0
