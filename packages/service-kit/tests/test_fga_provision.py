"""`fga.provision` must write the WHOLE model — the conditions block included.

The regression this pins: `model.json` gained a `conditions` block (time-boxed grants —
`non_expired_grant`) whose name several relations reference, and `provision` passed only
`schema_version` + `type_definitions` to `WriteAuthorizationModelRequest`. OpenFGA then rejects the
model ("condition non_expired_grant is undefined for relation reader"), the FGA client never builds,
and EVERY authorized route in every FGA-enabled service fail-closes 503 — the whole plane down, from
one silently dropped key.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from service_kit.governed import fga


class _Stores:
    stores: ClassVar[list[Any]] = []


class _Written:
    authorization_model_id = "model-1"


class _FakeClient:
    """Captures what `provision` writes. One instance per `async with` block."""

    requests: ClassVar[list[Any]] = []

    def __init__(self, configuration: Any) -> None:
        self._config = configuration

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_stores(self) -> _Stores:
        return _Stores()

    async def create_store(self, request: Any) -> Any:
        class _Created:
            id = "store-1"

        return _Created()

    async def write_authorization_model(self, request: Any) -> _Written:
        _FakeClient.requests.append(request)
        return _Written()


@pytest.mark.asyncio
async def test_provision_writes_the_conditions_block(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.requests = []
    monkeypatch.setattr(fga, "OpenFgaClient", _FakeClient)

    store_id, model_id = await fga.provision("http://fga:8080")

    assert (store_id, model_id) == ("store-1", "model-1")
    assert len(_FakeClient.requests) == 1
    request = _FakeClient.requests[0]
    model = fga.load_model()
    assert model.get("conditions"), "model.json no longer defines conditions — this test needs updating, not deleting"
    assert request.conditions == model["conditions"], (
        "provision dropped the conditions block — OpenFGA will 400 the model and every FGA-enabled service fail-closes 503"
    )
    assert request.type_definitions == model["type_definitions"]
    assert request.schema_version == model["schema_version"]
