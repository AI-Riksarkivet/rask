"""The ingest client's credential request must reach the catalog's door as the tier it asked for.

MEASURED in-cluster 2026-09-03. The ingest worker asked for a `write` credential, was handed a
READ one, and every fragment write was refused:

    PUT .../acme-bucket/c7688659_acme-bronze%24vendproof3/data/....lance
    403 Forbidden: <Code>AccessDenied</Code>

Both sides were individually correct and individually tested. `credentials.vend_credentials` declares
`tier: Annotated[Tier, Query()] = "read"`; `CatalogServiceClient.vend_storage_options` sends
`json={"tier": tier}`. FastAPI ignores an unknown body on a query parameter, so the door defaulted to
`read` and answered 200 with a perfectly valid credential — for the wrong tier.

TWO THINGS FAILED SILENTLY, and the second is worse than the refused write:

1. The write is refused at the object store, minutes later, as `AccessDenied` on a PUT — which reads
   as a missing grant on the table rather than as a request that asked for the wrong thing.
2. **The `can_write_data` check never ran.** The door gates it behind `if tier == "write"`, so a
   caller intending a write was authorized as a reader, and the audit record says so. The estate's
   own comment calls that decision "high-value" — it was not being made.

NEITHER SERVICE'S SUITE COULD SEE IT: each tested its own half against its own idea of the contract.
This lives in `tests/unit`, the one testpath that imports both, and asserts the CLIENT's request as
the DOOR parses it.
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

from catalog.core.vending import Tier
from ingest.catalog_service import CatalogServiceClient


@pytest.fixture
def door() -> FastAPI:
    """A stand-in for `vend_credentials` carrying ONLY its tier declaration, verbatim.

    Not the real door, because standing it up needs a namespace, FGA and an STS backend — and none of
    those participate in the defect. What is copied is the one line that does: how `tier` is declared.
    A drift between this and the real signature is caught by the assertion below.
    """
    app = FastAPI()

    @app.post("/v1/table/{id}/credentials")
    async def vend(id: str, tier: Annotated[Tier, Query()] = "read") -> dict[str, str]:
        return {"tier": tier}

    return app


def test_the_stand_in_matches_the_real_door_declaration() -> None:
    """If the real door moves `tier` into a body model, this file's premise is stale and must change
    with it rather than keep passing against a shape that no longer exists."""
    import inspect

    from catalog.api.v1.endpoints.credentials import vend_credentials

    parameter = inspect.signature(vend_credentials).parameters["tier"]
    assert "Query" in str(parameter.annotation), f"the door no longer takes `tier` as a query parameter: {parameter.annotation}"


def test_the_client_asks_for_the_write_tier_in_a_form_the_door_reads(door: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """The client's OWN request, replayed against the door. Nothing here supplies the tier on its
    behalf — that is the whole point, since the defect was the client supplying it somewhere the door
    does not look."""
    with TestClient(door) as transport:
        answered: dict[str, Any] = {}

        class _Shared:
            def post(self, url: str, **kwargs: Any) -> Any:
                path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
                response = transport.post(path, **{key: value for key, value in kwargs.items() if key in {"json", "params", "headers"}})
                answered["tier"] = response.json().get("tier")
                return response

        monkeypatch.setattr("ingest.http.shared_client", lambda: _Shared())
        CatalogServiceClient("http://catalog:2333").vend_storage_options("acme-bronze", "events", tier="write")

    assert answered["tier"] == "write", (
        f"the client asked for a write credential and the door parsed {answered['tier']!r} — "
        "the fragment PUTs are refused 403 AccessDenied and can_write_data is never checked"
    )


def test_a_write_tier_request_does_not_arrive_as_a_read(door: FastAPI) -> None:
    """The defect itself, stated as the property that was violated: a body-borne tier is INVISIBLE."""
    with TestClient(door) as transport:
        as_body = transport.post("/v1/table/acme-bronze$events/credentials", json={"tier": "write"})
        as_query = transport.post("/v1/table/acme-bronze$events/credentials", params={"tier": "write"})

    assert as_body.json()["tier"] == "read", "premise changed: the door now reads a body tier"
    assert as_query.json()["tier"] == "write"
