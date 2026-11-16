"""Lineage takes a signature on a person's behalf only from a listed delegator, and a signer it does not list at all.

[[LH-064]] C2. A signature binds an event to a key, and `onBehalfOf` lets the signer record a person as the author.
Only the identities the chart names as delegators (the catalog, which authenticated that person) may do that, and an
identity outside the chart's signer set is refused before lineage reads any key, since the facet's identity is the
sender's own claim. An announcement the catalog signed for the caller it authenticated, a write or a drop, is recorded
on that signature with no grant re-derived ([[LH-551]]): the catalog authorized it before it committed it, so a grant
revoked since, on its outputs or its inputs, takes nothing back. The commit reconciler's re-announcement of a lost one
is the same: the catalog signs it only for the author it recorded authorizing the commit ([[LH-550]], ADR 0187).

Driven through lineage's own app and lifespan, each event delivered to its ingest lane, FGA on. Public keys come from
the secret store, which the suite's double answers; signatures are built by the root conftest's `EventSigner` from the
wire format alone.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

import pytest
from lineage_lane import Feed, governed, started
from openfga_sdk import OpenFgaClient

from lakehouse.bus_delivery import Outcome, Settlement
from lakehouse.governed.decider import OpenFgaDecider
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


def _everything(_object: str) -> bool:
    return True


def _nothing(_object: str) -> bool:
    return False


def _answering(monkeypatch: pytest.MonkeyPatch, *, held: Callable[[str], bool], governed_objects: Callable[[str], bool]) -> None:
    """OpenFGA answering ``held`` for every check and holding a tuple on each object ``governed_objects`` names."""
    from lakehouse.governed import fga

    async def answer(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        del user, relation
        return {obj: held(obj) for obj in objects}

    async def tuples(_client: object, obj: str) -> list[object]:
        return [object()] if governed_objects(obj) else []

    monkeypatch.setattr(fga, "batch_check", answer)
    monkeypatch.setattr(fga, "read_object_tuples", tuples)


@pytest.mark.parametrize(
    ("case", "held", "governed_objects", "recorded"),
    [
        pytest.param("stage-for-a-person", _everything, _everything, False, id="a-signer-that-is-not-a-delegator-signing-for-a-person"),
        pytest.param("catalog-for-a-person", _nothing, _everything, True, id="the-delegator-signing-for-a-person-whose-grants-were-revoked-since"),
        pytest.param("unlisted-signer", _everything, _everything, False, id="a-signer-the-chart-does-not-list"),
        pytest.param("catalog-drop", _nothing, _nothing, True, id="a-catalog-drop-its-signature-verifies"),
        pytest.param("forged-catalog-drop", _nothing, _nothing, False, id="a-catalog-drop-its-signature-does-not-verify"),
        pytest.param("pipeline-own-run", _nothing, _nothing, False, id="a-non-delegators-write-whose-output-holds-no-tuple"),
    ],
)
def test_lineage_takes_a_delegated_signature_only_from_the_listed_delegator(  # noqa: PLR0913 - parametrized
    monkeypatch: pytest.MonkeyPatch,
    event_signer: Any,
    secret_store: SecretStoreDouble,
    respx_allows_unused_routes: None,
    case: str,
    held: Callable[[str], bool],
    governed_objects: Callable[[str], bool],
    recorded: bool,
) -> None:
    governed(monkeypatch)
    monkeypatch.setenv("LINEAGE_SIGNERS", json.dumps([CATALOG, STAGE, PIPELINE]))
    monkeypatch.setenv("LINEAGE_DELEGATORS", json.dumps([CATALOG]))
    _answering(monkeypatch, held=held, governed_objects=governed_objects)
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

    with started(monkeypatch, Feed(), decider=OpenFgaDecider(cast("OpenFgaClient", object()))) as lineage:
        settled = lineage.deliver({"data": event})

    assert settled == Settlement(outcome=Outcome.TAKEN), f"{case}: the lane settled {settled}"
    assert bool(lineage.feed.recorded) is recorded, f"{case}: recorded {lineage.feed.recorded}"
    assert not unlisted_read.called, "lineage read a key for a signer the chart does not list"
