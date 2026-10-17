"""Lineage takes a signature on a person's behalf only from a listed delegator, and a signer it does not list at all.

[[LH-064]] C2. A signature binds an event to a key, and `onBehalfOf` lets the signer record a person as the author.
Only the identities the chart names as delegators (the catalog, which authenticated that person) may do that, and an
identity outside the chart's signer set is refused before lineage reads any key, since the facet's identity is the
sender's own claim. The catalog's own DROP is admitted on its signature alone, with no grant check, because the
catalog revoked the table's grants in the same request: so only a catalog signature this delivery verified may do it.
A rename's register is admitted on the same signature for its input, the renamed-away source, whose grants the same
request moved to the destination (owner ruling 2026-10-17, LH-501); its output, the destination, keeps the check.

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
#: A listed signer that is not a delegator, named as the estate names one: a service account, which is a principal.
SERVICE = "kubernetes~system:serviceaccount:default:rask-sa-maintenance"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"
#: A table renamed within its namespace: the catalog names each by its parent namespace and canonical id.
SOURCE, DESTINATION = "acme-silver$orders", "acme-silver$orders-renamed"


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


def _register_after_a_rename(author: str = PERSON) -> dict[str, Any]:
    """The destination's register a rename announces, its renamed-away source as its input, shaped as the catalog's
    rename door builds it."""
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-10-02T12:00:00+00:00",
        "run": {
            "runId": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5d",
            "facets": {"lance": {"operation": "register_table"}, "author": {"name": author, "sub": author}},
        },
        "job": {"namespace": "lance", "name": "catalog.register_table"},
        "inputs": [{"namespace": "acme-silver", "name": SOURCE}],
        "outputs": [{"namespace": "acme-silver", "name": DESTINATION}],
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
    }


def _everything(_object: str) -> bool:
    return True


def _nothing(_object: str) -> bool:
    return False


def _only_the_destination(object_: str) -> bool:
    """What a rename leaves its author holding: every grant on the source moved to the destination."""
    return object_ == f"table:{DESTINATION}"


def _door(monkeypatch: pytest.MonkeyPatch, *, held: Callable[[str], bool]) -> tuple[TestClient, _Feed]:
    for key, value in {
        "APP_API_TOKEN": "the-estate-app-token",
        "LINEAGE_DAPR_ENABLED": "true",
        "DAPR_HTTP_PORT": "3500",
        "RASK_FGA_ENABLED": "true",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": "https://idp.invalid/dex",
        "RASK_OIDC_AUDIENCE": "rask",
        "RASK_OIDC_CONNECTORS": json.dumps({"local": "dex"}),
        "LINEAGE_SIGNERS": json.dumps([CATALOG, STAGE, SERVICE]),
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

    monkeypatch.setattr(fga, "batch_check", answer)
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
    ("case", "held", "status", "recorded"),
    [
        pytest.param("stage-for-a-person", _everything, "SUCCESS", False, id="a-signer-that-is-not-a-delegator-signing-for-a-person"),
        pytest.param("catalog-for-a-person", _everything, "SUCCESS", True, id="the-delegator-signing-for-a-person"),
        pytest.param("unlisted-signer", _everything, "SUCCESS", False, id="a-signer-the-chart-does-not-list"),
        pytest.param("catalog-drop", _nothing, "SUCCESS", True, id="a-catalog-drop-its-signature-verifies"),
        pytest.param("forged-catalog-drop", _nothing, "SUCCESS", False, id="a-catalog-drop-its-signature-does-not-verify"),
        pytest.param("catalog-rename", _only_the_destination, "SUCCESS", True, id="a-catalog-rename-whose-source-holds-no-grant"),
        pytest.param("catalog-rename", _nothing, "DROP", False, id="a-catalog-rename-onto-a-table-its-author-may-not-write"),
        pytest.param("service-rename", _only_the_destination, "DROP", False, id="a-rename-register-a-signer-that-is-not-a-delegator-signed"),
    ],
)
def test_lineage_takes_a_delegated_drop_or_rename_signature_only_from_the_listed_delegator(  # noqa: PLR0913 - parametrized
    monkeypatch: pytest.MonkeyPatch,
    event_signer: Any,
    respx_allows_unused_routes: None,
    case: str,
    held: Callable[[str], bool],
    status: str,
    recorded: bool,
) -> None:
    client, feed = _door(monkeypatch, held=held)
    catalog, stage, unlisted, impostor = event_signer(CATALOG), event_signer(STAGE), event_signer(UNLISTED), event_signer(CATALOG)
    service = event_signer(SERVICE)
    for signer in (catalog, stage, service):
        respx.get(f"{SECRETS}/{signing_public_secret(signer.identity)}").mock(return_value=httpx.Response(200, json={"keys": signer.public}))
    unlisted_read = respx.get(f"{SECRETS}/signing-public-{UNLISTED}").mock(return_value=httpx.Response(200, json={"keys": unlisted.public}))
    event = {
        "stage-for-a-person": lambda: stage.sign(_run(PERSON), on_behalf_of=PERSON),
        "catalog-for-a-person": lambda: catalog.sign(_run(PERSON), on_behalf_of=PERSON),
        "unlisted-signer": lambda: unlisted.sign(_run(UNLISTED)),
        "catalog-drop": lambda: catalog.sign(_drop(), on_behalf_of=PERSON),
        "forged-catalog-drop": lambda: impostor.sign(_drop(), on_behalf_of=PERSON),
        "catalog-rename": lambda: catalog.sign(_register_after_a_rename(), on_behalf_of=PERSON),
        "service-rename": lambda: service.sign(_register_after_a_rename(author=SERVICE)),
    }[case]()

    answered = client.post("/lineage-events", json={"data": event}, headers={"dapr-api-token": "the-estate-app-token"})

    assert answered.json() == {"status": status}, f"{case}: the door answered {answered.json()}"
    assert bool(feed.recorded) is recorded, f"{case}: recorded {feed.recorded}"
    assert not unlisted_read.called, "lineage read a key for a signer the chart does not list"
