"""The probe that would have caught an unscoped actor state store before a person did.

Every payload here is the SHAPE the live sidecar returned on 2026-08-24 — the scoped case was read
off `medallion-producer` after the scope was repaired, the unscoped case is that same response with
the component absent, which is exactly how daprd represents "not scoped for this app-id".
"""

import json
from typing import Any

import pytest

from service_kit.governed import actor_state_store


#: `lance-statestore` as the producer's own sidecar reported it once it could see it.
_SCOPED: dict[str, Any] = {
    "actorRuntime": {"runtimeStatus": "RUNNING", "hostReady": True},
    "components": [
        {"name": "lance-secrets", "type": "secretstores.hashicorp.vault", "capabilities": []},
        {
            "name": "lance-statestore",
            "type": "state.postgresql",
            "capabilities": ["ETAG", "TRANSACTIONAL", "TTL", "KEYS_LIKE", "QUERY_API", "ACTOR"],
        },
    ],
}

#: The same sidecar while the app-id was missing from the Component's `scopes`. Note what is NOT
#: here: any error, any flag, any hint. The component is simply absent.
_UNSCOPED: dict[str, Any] = {
    "actorRuntime": {"runtimeStatus": "RUNNING", "hostReady": True},
    "components": [{"name": "lance-secrets", "type": "secretstores.hashicorp.vault", "capabilities": []}],
}


def _serve(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | None, *, boom: Exception | None = None) -> None:
    class _Response:
        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode()

    def _urlopen(*_: object, **__: object) -> _Response:
        if boom is not None:
            raise boom
        return _Response()

    monkeypatch.setattr(actor_state_store.urllib.request, "urlopen", _urlopen)


@pytest.mark.asyncio
async def test_a_scoped_actor_state_store_is_reported_available(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _SCOPED)
    assert await actor_state_store.probe_actor_state_store(capability="held promotions cannot be reviewed") is True


@pytest.mark.asyncio
async def test_an_unscoped_actor_state_store_is_caught(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The defect itself. The sidecar answers happily; only the missing capability gives it away."""
    _serve(monkeypatch, _UNSCOPED)
    with caplog.at_level("ERROR"):
        assert await actor_state_store.probe_actor_state_store(capability="held promotions cannot be reviewed") is False
    assert "NO ACTOR STATE STORE" in caplog.text


@pytest.mark.asyncio
async def test_the_error_names_the_consequence_and_the_restart(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The whole point is the line an operator reads.

    daprd already logs the mechanism ("actor hosting disabled") and then contradicts it with
    "Workflow engine started". This line must carry what the OTHER two do not: what breaks, and that
    a scope added under a running sidecar does not reach it without a restart.
    """
    _serve(monkeypatch, _UNSCOPED)
    with caplog.at_level("ERROR"):
        await actor_state_store.probe_actor_state_store(capability="held promotions cannot be reviewed")
    assert "held promotions cannot be reviewed" in caplog.text
    assert "RESTART" in caplog.text
    assert "stateStore.scopes" in caplog.text


@pytest.mark.asyncio
async def test_a_component_without_the_actor_capability_does_not_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain state store is not an actor state store, and the difference is the whole bug.

    `lance-statestore` is ONE component that is both; a deployment could scope a different, non-actor
    store to this app-id and the naive check ("is any state store visible") would pass while every
    workflow call still failed.
    """
    _serve(
        monkeypatch,
        {"components": [{"name": "plain-cache", "type": "state.redis", "capabilities": ["ETAG", "TRANSACTIONAL", "TTL"]}]},
    )
    assert await actor_state_store.probe_actor_state_store(capability="x") is False


@pytest.mark.asyncio
async def test_an_unreachable_sidecar_is_not_read_as_healthy(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A probe that could not be asked returns False — but as a WARNING, not the misconfiguration ERROR.

    At lifespan time a sidecar that has not finished booting is ordinary. Reporting that as the
    scoping defect would train an operator to ignore the line that matters.
    """
    _serve(monkeypatch, None, boom=OSError("connection refused"))
    with caplog.at_level("WARNING"):
        assert await actor_state_store.probe_actor_state_store(capability="held promotions cannot be reviewed") is False
    assert "NO ACTOR STATE STORE" not in caplog.text
