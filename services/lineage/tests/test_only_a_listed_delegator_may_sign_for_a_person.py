"""Lineage takes a signature on a person's behalf only from a listed delegator, and a signer it does not list at all.

[[LH-064]] C2. A signature binds an event to a key, and `onBehalfOf` lets the signer record a person as the author.
Only the identities the chart names as delegators (the catalog, which authenticated that person) may do that, and an
identity outside the chart's signer set is refused before lineage reads any key, since the facet's identity is the
sender's own claim. An announcement the catalog signed for the caller it authenticated, a write or a drop, is recorded
on that signature with no grant re-derived ([[LH-551]]): the catalog authorized it before it committed it, so a grant
revoked since, on its outputs or its inputs, takes nothing back. The commit reconciler's re-announcement of a lost one
is the same: the catalog signs it only for the author it recorded authorizing the commit ([[LH-550]], ADR 0187).

Driven through lineage's own app and lifespan, each event delivered to its ingest lane, every check the real model's on
an OpenFGA store of the test's own: the event's author holding the rungs its write needs, another person holding the
tables so they are governed, or nobody holding anything. Public keys come from
the secret store, which the suite's double answers; signatures are built by the root conftest's `EventSigner` from the
wire format alone.
"""

import json
from typing import Any, Literal

import pytest
from lineage_lane import Feed, governed, started

from lakehouse.events.bus_delivery import Outcome, Settlement
from lakehouse.governed.fga_ids import user_object
from lakehouse.governed.signing_key import signing_public_secret
from tests.secret_store_double import SecretStoreDouble


CATALOG, STAGE, UNLISTED, PERSON = "service-catalog", "service-pipeline-a", "service-trainer", "dex~alice"
#: A listed signer that is no delegator and signs its own runs, as the principal it is.
PIPELINE = "kubernetes~system:serviceaccount:etl:etl-runner"
#: The uuids the catalog minted for the run's output, the table it read and the table a drop names, which its events carry
#: in each dataset's `rask_lance` facet (ADR 0173 §10).
EVENTS_UUID = "5d0f6a1e-8b2c-4f3d-9e7a-1c2b3d4e5f60"
SOURCE_UUID = "8e1f2a3b-4c5d-4e6f-8a7b-9c0d1e2f3a4b"
DROPPED_UUID = "2b3c4d5e-6f70-4182-93a4-b5c6d7e8f901"


def _run(author: str) -> dict[str, Any]:
    """A write to one governed table that read another: where the gate checks the author's grants, it checks both."""
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-10-02T12:00:00+00:00",
        "run": {
            "runId": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c",
            "facets": {"rask_author": {"name": author, "sub": author}, "rask_lance": {"operation": "insert"}},
        },
        "job": {"namespace": "lance", "name": "insert.acme-bronze$events"},
        "inputs": [{"namespace": "bronze", "name": "acme-bronze$source", "facets": {"rask_lance": {"tableUuid": SOURCE_UUID}}}],
        "outputs": [{"namespace": "bronze", "name": "acme-bronze$events", "facets": {"rask_lance": {"tableUuid": EVENTS_UUID}}}],
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
    }


def _drop() -> dict[str, Any]:
    return {
        "eventTime": "2026-10-02T12:00:00+00:00",
        "dataset": {
            "namespace": "lance",
            "name": "acme-bronze$dropped",
            "facets": {
                "rask_lance": {"operation": "drop_table", "tableUuid": DROPPED_UUID},
                "rask_author": {"name": PERSON, "sub": PERSON},
            },
        },
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/DatasetEvent",
    }


#: Who holds the tables an event names: its author the rungs its write needs, another person both tables, or nobody.
type Holders = Literal["author", "another", "nobody"]


def _hold(store: Any, holders: Holders, author: str) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """Seed ``store`` as ``holders`` says, for the event ``author`` names."""
    if holders == "author":
        store.write((user_object(author), "writer", f"table:{EVENTS_UUID}"), (user_object(author), "reader", f"table:{SOURCE_UUID}"))
    elif holders == "another":
        store.write(*((user_object("dex~bob"), "owner", f"table:{uuid}") for uuid in (EVENTS_UUID, SOURCE_UUID)))


@pytest.mark.parametrize(
    ("case", "holders", "recorded"),
    [
        pytest.param("stage-for-a-person", "author", False, id="a-signer-that-is-not-a-delegator-signing-for-a-person"),
        pytest.param("catalog-for-a-person", "another", True, id="the-delegator-signing-for-a-person-whose-grants-were-revoked-since"),
        pytest.param("unlisted-signer", "another", False, id="a-signer-the-chart-does-not-list"),
        pytest.param("catalog-drop", "nobody", True, id="a-catalog-drop-its-signature-verifies"),
        pytest.param("forged-catalog-drop", "nobody", False, id="a-catalog-drop-its-signature-does-not-verify"),
        pytest.param("pipeline-own-run", "nobody", False, id="a-non-delegators-write-whose-output-holds-no-tuple"),
    ],
)
def test_lineage_takes_a_delegated_signature_only_from_the_listed_delegator(  # noqa: PLR0913 - parametrized
    monkeypatch: pytest.MonkeyPatch,
    event_signer: Any,
    secret_store: SecretStoreDouble,
    respx_allows_unused_routes: None,
    openfga_store: Any,
    case: str,
    holders: Holders,
    recorded: bool,
) -> None:
    governed(monkeypatch, openfga_store)
    monkeypatch.setenv("LINEAGE_SIGNERS", json.dumps([CATALOG, STAGE, PIPELINE]))
    monkeypatch.setenv("LINEAGE_DELEGATORS", json.dumps([CATALOG]))
    _hold(openfga_store, holders, PIPELINE if case == "pipeline-own-run" else PERSON)
    catalog, stage, unlisted, impostor, pipeline = (event_signer(name) for name in (CATALOG, STAGE, UNLISTED, CATALOG, PIPELINE))
    for signer in (catalog, stage, pipeline):
        secret_store.hold(signing_public_secret(signer.identity), {"keys": signer.public})
    unlisted_read = secret_store.hold(f"signing-public-{UNLISTED}", {"keys": unlisted.public})
    event = {
        "stage-for-a-person": lambda: stage.sign(_run(PERSON), on_behalf_of=PERSON),
        "catalog-for-a-person": lambda: catalog.sign(_run(PERSON), on_behalf_of=PERSON),
        "unlisted-signer": lambda: unlisted.sign(_run(UNLISTED)),
        "catalog-drop": lambda: catalog.sign(_drop(), on_behalf_of=PERSON),
        "forged-catalog-drop": lambda: impostor.sign(_drop(), on_behalf_of=PERSON),
        "pipeline-own-run": lambda: pipeline.sign(_run(PIPELINE)),
    }[case]()

    with started(monkeypatch, Feed()) as lineage:
        settled = lineage.deliver({"data": event})

    assert settled == Settlement(outcome=Outcome.TAKEN), f"{case}: the lane settled {settled}"
    assert bool(lineage.feed.recorded) is recorded, f"{case}: recorded {lineage.feed.recorded}"
    assert not unlisted_read.called, "lineage read a key for a signer the chart does not list"
