"""Lineage takes a signature on a person's behalf only from a listed delegator, and a signer it does not list at all.

[[LH-064]] C2. A signature binds an event to a key, and `onBehalfOf` lets the signer record a person as the author.
Only the identities the chart names as delegators (the catalog, which authenticated that person) may do that, and an
identity outside the chart's signer set is refused before lineage reads any key, since the facet's identity is the
sender's own claim. The catalog's own DROP is admitted on its signature alone, with no grant check, because the
catalog revoked the table's grants in the same request: so only a catalog signature this delivery verified may do it.
A write the catalog signed whose output it has since dropped or deregistered is admitted on the same signature
([[LH-502]]): its output holds no tuple at all, and the catalog authorized the write before it committed it. An output
that is governed and merely not writable by the author keeps the refusal.

Driven through the registered `/lineage-events` route, FGA on. Public keys come from the sidecar's secret API,
stood in for by respx; signatures are built by the root conftest's `EventSigner` from the wire format alone.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
import respx
from fastapi import FastAPI
from openfga_sdk import OpenFgaClient
from starlette.testclient import TestClient

from lineage.models import DatasetEvent
from service_kit.governed.decider import OpenFgaDecider
from service_kit.governed.signing_key import signing_public_secret


CATALOG, STAGE, UNLISTED, PERSON = "service-catalog", "service-bronze-to-silver", "service-trainer", "dex~alice"
#: A listed signer that is no delegator and signs its own runs, as the principal it is.
PIPELINE = "kubernetes~system:serviceaccount:etl:etl-runner"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"
#: The uuid the catalog minted for the run's output, which its events carry in the output's `rask_lance` facet (ADR 0173 §10).
EVENTS_UUID = "5d0f6a1e-8b2c-4f3d-9e7a-1c2b3d4e5f60"


class _Feed:
    def __init__(self) -> None:
        self.recorded: list[str] = []

    async def record(self, event: Any) -> None:
        self.recorded.append(event.dataset.name if isinstance(event, DatasetEvent) else event.run_id)

    async def run_author(self, _run_id: str) -> str | None:
        return None

    async def recorded_event(self, _run_id: str, _event_type: str | None) -> dict[str, Any] | None:
        return None


def _run(author: str) -> dict[str, Any]:
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-10-02T12:00:00+00:00",
        "run": {"runId": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c", "facets": {"rask_author": {"name": author, "sub": author}}},
        "job": {"namespace": "lance", "name": "catalog.insert"},
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
            "facets": {"rask_lance": {"operation": "drop_table"}, "rask_author": {"name": PERSON, "sub": PERSON}},
        },
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/DatasetEvent",
    }


def _everything(_object: str) -> bool:
    return True


def _nothing(_object: str) -> bool:
    return False


def _door(monkeypatch: pytest.MonkeyPatch, *, held: Callable[[str], bool], governed: Callable[[str], bool]) -> tuple[TestClient, _Feed]:
    for key, value in {
        "APP_API_TOKEN": "the-estate-app-token",
        "LINEAGE_DAPR_ENABLED": "true",
        "DAPR_HTTP_PORT": "3500",
        "RASK_FGA_ENABLED": "true",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": "https://idp.invalid/dex",
        "RASK_OIDC_AUDIENCE": "rask",
        "RASK_OIDC_CONNECTORS": json.dumps({"local": "dex"}),
        "LINEAGE_SIGNERS": json.dumps([CATALOG, STAGE, PIPELINE]),
        "LINEAGE_DELEGATORS": json.dumps([CATALOG]),
    }.items():
        monkeypatch.setenv(key, value)

    from lineage.api.dapr import register_dapr
    from lineage.core.config import get_settings
    from service_kit.governed import fga
    from service_kit.lakehouse.ns_errors import install_problem_handlers

    async def answer(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        del user, relation
        return {obj: held(obj) for obj in objects}

    async def tuples(_client: object, obj: str) -> list[object]:
        return [object()] if governed(obj) else []

    monkeypatch.setattr(fga, "batch_check", answer)
    monkeypatch.setattr(fga, "read_object_tuples", tuples)
    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    register_dapr(app)
    feed = _Feed()
    app.state.provenance = feed
    app.state.decider = OpenFgaDecider(cast("OpenFgaClient", object()))
    return TestClient(app), feed


@respx.mock
@pytest.mark.parametrize(
    ("case", "held", "governed", "status", "recorded"),
    [
        pytest.param("stage-for-a-person", _everything, _everything, "SUCCESS", False, id="a-signer-that-is-not-a-delegator-signing-for-a-person"),
        pytest.param("catalog-for-a-person", _everything, _everything, "SUCCESS", True, id="the-delegator-signing-for-a-person"),
        pytest.param("unlisted-signer", _everything, _everything, "SUCCESS", False, id="a-signer-the-chart-does-not-list"),
        pytest.param("catalog-drop", _nothing, _nothing, "SUCCESS", True, id="a-catalog-drop-its-signature-verifies"),
        pytest.param("forged-catalog-drop", _nothing, _nothing, "SUCCESS", False, id="a-catalog-drop-its-signature-does-not-verify"),
        pytest.param("catalog-for-a-person", _nothing, _nothing, "SUCCESS", True, id="a-catalog-write-whose-output-was-since-dropped"),
        pytest.param("catalog-for-a-person", _nothing, _everything, "DROP", False, id="a-catalog-write-to-a-governed-output-its-author-may-not-write"),
        pytest.param("pipeline-own-run", _nothing, _nothing, "SUCCESS", False, id="a-non-delegators-write-whose-output-holds-no-tuple"),
    ],
)
def test_lineage_takes_a_delegated_signature_only_from_the_listed_delegator(  # noqa: PLR0913 - parametrized
    monkeypatch: pytest.MonkeyPatch,
    event_signer: Any,
    respx_allows_unused_routes: None,
    case: str,
    held: Callable[[str], bool],
    governed: Callable[[str], bool],
    status: str,
    recorded: bool,
) -> None:
    client, feed = _door(monkeypatch, held=held, governed=governed)
    catalog, stage, unlisted, impostor, pipeline = (event_signer(name) for name in (CATALOG, STAGE, UNLISTED, CATALOG, PIPELINE))
    for signer in (catalog, stage, pipeline):
        respx.get(f"{SECRETS}/{signing_public_secret(signer.identity)}").mock(return_value=httpx.Response(200, json={"keys": signer.public}))
    unlisted_read = respx.get(f"{SECRETS}/signing-public-{UNLISTED}").mock(return_value=httpx.Response(200, json={"keys": unlisted.public}))
    event = {
        "stage-for-a-person": lambda: stage.sign(_run(PERSON), on_behalf_of=PERSON),
        "catalog-for-a-person": lambda: catalog.sign(_run(PERSON), on_behalf_of=PERSON),
        "unlisted-signer": lambda: unlisted.sign(_run(UNLISTED)),
        "catalog-drop": lambda: catalog.sign(_drop(), on_behalf_of=PERSON),
        "forged-catalog-drop": lambda: impostor.sign(_drop(), on_behalf_of=PERSON),
        "pipeline-own-run": lambda: pipeline.sign(_run(PIPELINE)),
    }[case]()

    answered = client.post("/lineage-events", json={"data": event}, headers={"dapr-api-token": "the-estate-app-token"})

    assert answered.json() == {"status": status}, f"{case}: the door answered {answered.json()}"
    assert bool(feed.recorded) is recorded, f"{case}: recorded {feed.recorded}"
    assert not unlisted_read.called, "lineage read a key for a signer the chart does not list"
