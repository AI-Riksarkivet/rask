"""Lineage takes a signature on a person's behalf only from a listed delegator, and a signer it does not list at all.

[[LH-064]] C2. A signature binds an event to a key, and `onBehalfOf` lets the signer record a person as the author.
Only the identities the chart names as delegators (the catalog, which authenticated that person) may do that, and an
identity outside the chart's signer set is refused before lineage reads any key, since the facet's identity is the
sender's own claim. The catalog's own DROP is admitted on its signature alone, with no grant check, because the
catalog revoked the table's grants in the same request: so only a catalog signature this delivery verified may do it.

Driven through the registered `/lineage-events` route, FGA on. Public keys come from the sidecar's secret API,
stood in for by respx; signatures are built by the root conftest's `EventSigner` from the wire format alone.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from starlette.testclient import TestClient


CATALOG, STAGE, UNLISTED, PERSON = "service-catalog", "service-bronze-to-silver", "service-trainer", "CgVhbGljZRIFbG9jYWw"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"


class _Feed:
    def __init__(self) -> None:
        self.recorded: list[str] = []

    async def ingest_event(self, event: Any) -> None:
        self.recorded.append(event.run_id)

    async def ingest_dataset_event(self, event: Any) -> None:
        self.recorded.append(event.dataset.name)

    async def run_output_names(self, _run_id: str) -> list[str]:
        return []

    async def recorded_event(self, _run_id: str, _event_type: str | None) -> dict[str, Any] | None:
        return None


def _run(author: str) -> dict[str, Any]:
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-10-02T12:00:00+00:00",
        "run": {"runId": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c", "facets": {"author": {"name": author, "sub": author}}},
        "job": {"namespace": "lance", "name": "catalog.insert"},
        "outputs": [{"namespace": "bronze", "name": "acme-bronze$events"}],
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
    }


def _drop() -> dict[str, Any]:
    return {
        "eventTime": "2026-10-02T12:00:00+00:00",
        "dataset": {
            "namespace": "lance",
            "name": "acme-bronze$dropped",
            "facets": {"lance": {"operation": "drop_table"}, "author": {"name": PERSON, "sub": PERSON}},
        },
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/DatasetEvent",
    }


def _door(monkeypatch: pytest.MonkeyPatch, *, grant: bool) -> tuple[TestClient, _Feed]:
    for key, value in {
        "APP_API_TOKEN": "the-estate-app-token",
        "LINEAGE_DAPR_ENABLED": "true",
        "DAPR_HTTP_PORT": "3500",
        "RASK_FGA_ENABLED": "true",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": "https://idp.invalid/dex",
        "RASK_OIDC_AUDIENCE": "rask",
        "LINEAGE_SIGNERS": json.dumps([CATALOG, STAGE]),
        "LINEAGE_DELEGATORS": json.dumps([CATALOG]),
    }.items():
        monkeypatch.setenv(key, value)

    from lineage.api.dapr import register_dapr
    from lineage.core.config import get_settings
    from service_kit.governed import fga
    from service_kit.lakehouse.ns_errors import install_problem_handlers

    async def answer(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        del user, relation
        return dict.fromkeys(objects, grant)

    monkeypatch.setattr(fga, "batch_check", answer)
    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    register_dapr(app)
    feed = _Feed()
    app.state.repository = feed
    app.state.fga = object()
    return TestClient(app), feed


@respx.mock
@pytest.mark.parametrize(
    ("case", "grant", "status", "recorded"),
    [
        pytest.param("stage-for-a-person", True, "SUCCESS", False, id="a-signer-that-is-not-a-delegator-signing-for-a-person"),
        pytest.param("catalog-for-a-person", True, "SUCCESS", True, id="the-delegator-signing-for-a-person"),
        pytest.param("unlisted-signer", True, "SUCCESS", False, id="a-signer-the-chart-does-not-list"),
        pytest.param("catalog-drop", False, "SUCCESS", True, id="a-catalog-drop-its-signature-verifies"),
        pytest.param("forged-catalog-drop", False, "SUCCESS", False, id="a-catalog-drop-its-signature-does-not-verify"),
    ],
)
def test_lineage_takes_a_delegated_or_drop_signature_only_from_the_listed_delegator(  # noqa: PLR0913 - parametrized
    monkeypatch: pytest.MonkeyPatch, event_signer: Any, respx_allows_unused_routes: None, case: str, grant: bool, status: str, recorded: bool
) -> None:
    client, feed = _door(monkeypatch, grant=grant)
    catalog, stage, unlisted, impostor = event_signer(CATALOG), event_signer(STAGE), event_signer(UNLISTED), event_signer(CATALOG)
    for signer in (catalog, stage):
        respx.get(f"{SECRETS}/signing-public-{signer.identity}").mock(return_value=httpx.Response(200, json={"keys": signer.public}))
    unlisted_read = respx.get(f"{SECRETS}/signing-public-{UNLISTED}").mock(return_value=httpx.Response(200, json={"keys": unlisted.public}))
    event = {
        "stage-for-a-person": lambda: stage.sign(_run(PERSON), on_behalf_of=PERSON),
        "catalog-for-a-person": lambda: catalog.sign(_run(PERSON), on_behalf_of=PERSON),
        "unlisted-signer": lambda: unlisted.sign(_run(UNLISTED)),
        "catalog-drop": lambda: catalog.sign(_drop(), on_behalf_of=PERSON),
        "forged-catalog-drop": lambda: impostor.sign(_drop(), on_behalf_of=PERSON),
    }[case]()

    answered = client.post("/lineage-events", json={"data": event}, headers={"dapr-api-token": "the-estate-app-token"})

    assert answered.json() == {"status": status}, f"{case}: the door answered {answered.json()}"
    assert bool(feed.recorded) is recorded, f"{case}: recorded {feed.recorded}"
    assert not unlisted_read.called, "lineage read a key for a signer the chart does not list"
