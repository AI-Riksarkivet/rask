"""After a key rotation lineage still verifies an event signed with the identity's previous key, and no unpublished one.

[[LH-064]] C3. A rotation publishes the identity's new public key first and keeps the previous one, so an event signed
before the rotation (still in the 168 h stream, still staged in the outbox, or from a signer not yet restarted)
verifies. A key the identity's published list does not hold is refused once lineage has re-read the list, so an
unknown key id is never decided on a stale cache.

Driven through lineage's own app and lifespan, each event delivered to its ingest lane, with signing enforced and FGA
off. The published list comes from the secret store, which the suite's double answers; signatures are built by the root
conftest's `EventSigner`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from lineage_lane import Feed, started

from lakehouse.events.bus_delivery import Outcome, Settlement
from tests.secret_store_double import SecretStoreDouble


SIGNER = "service-maintenance"


def _event() -> dict[str, Any]:
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-10-02T12:00:00+00:00",
        "run": {"runId": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5d", "facets": {"rask_author": {"name": SIGNER, "sub": SIGNER}}},
        "job": {"namespace": "maintenance", "name": "compaction.acme-silver"},
        "outputs": [{"namespace": "silver", "name": "acme-silver$events"}],
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
    }


@pytest.mark.parametrize(
    ("signed_with", "recorded"),
    [
        pytest.param("previous", True, id="the-previous-key-after-a-rotation"),
        pytest.param("unpublished", False, id="a-key-the-published-list-does-not-hold"),
    ],
)
def test_lineage_verifies_the_previous_key_after_a_rotation_and_refuses_an_unpublished_one(
    monkeypatch: pytest.MonkeyPatch, event_signer: Any, secret_store: SecretStoreDouble, signed_with: str, recorded: bool
) -> None:
    monkeypatch.setenv("LINEAGE_SIGNERS", json.dumps([SIGNER]))
    monkeypatch.setenv("LINEAGE_DELEGATORS", json.dumps([]))
    current, previous, unpublished = event_signer(SIGNER), event_signer(SIGNER), event_signer(SIGNER)
    secret_store.hold(f"signing-public-{SIGNER}", {"keys": f"{current.public},{previous.public}"})
    event = (previous if signed_with == "previous" else unpublished).sign(_event())

    with started(monkeypatch, Feed()) as lineage:
        settled = lineage.deliver({"data": event})

    assert settled == Settlement(outcome=Outcome.TAKEN), f"{signed_with}: the lane settled {settled}"
    assert bool(lineage.feed.recorded) is recorded, f"{signed_with}: recorded {lineage.feed.recorded}"
