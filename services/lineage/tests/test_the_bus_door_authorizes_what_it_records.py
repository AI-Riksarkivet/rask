"""The bus door authorizes the stamped subject, as the HTTP door does (§ E2).

THE ASYMMETRY THIS CLOSES. `endpoints/ingest.py` applies `enforce_author` (bind the run to the verified
principal) and `enforce_output_authz` (may that principal write these outputs). `api/dapr.py` applied
neither: it was guarded only by the sidecar's shared app-api-token, which authenticates a TRANSPORT and
not a producer. Any holder of that one credential could therefore record any provenance about any
dataset — including an `operation: drop_table` on a table it had never seen, which the reconcile sweep
then honours.

THE ORDERING RULE THIS ROW SET, and why the gate is only now safe: "a consumer-side gate is only safe
once the producers sign". Measured 2026-09-08, 664 of 5 644 runs carried no author and 518 of those were
the maintenance sweep's, so this gate would have silently deleted the whole maintenance plane's
provenance. Re-measured 2026-09-09 after the producer half landed: every maintenance run since the roll
carries `service-maintenance`, and the only unauthored runs left are e2e fixture rows written straight
to the repository, which never pass through this door.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from lance_namespace import PermissionDeniedError

from lineage.api.fga_deps import relations_for_operation
from lineage.models import RunEvent
from lineage.services.consumer import handle_cloud_event


def _event(**run_facets: Any) -> RunEvent:
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-09T00:00:00+00:00",
            "run": {"runId": "11111111-1111-1111-1111-111111111111", "facets": run_facets},
            "job": {"namespace": "bus", "name": "probe"},
            "outputs": [{"namespace": "bronze", "name": "bronze$pages"}],
        }
    )


class _Repo:
    def __init__(self) -> None:
        self.ingested: list[RunEvent] = []

    async def ingest_event(self, event: RunEvent) -> None:
        self.ingested.append(event)


class _RepoWithOutputs(_Repo):
    """The capturing double plus BOTH reads the authz path makes: the run-mutation lookup and the
    replay comparison.

    A double that LACKS either does not fail the same way — the AttributeError lands in the consumer's
    outage branch and the delivery answers RETRY, so the test would pass for the wrong reason and could
    never tell a refusal from a broken double. `recorded_event` answers None: the route test drives a
    FIRST delivery, which has nothing to have replayed.
    """

    async def run_output_names(self, _run_id: str) -> list[str]:
        return []

    async def recorded_event(self, _run_id: str, _event_type: str | None) -> dict[str, Any] | None:
        return None


def test_a_DATA_write_demands_the_writer_rung() -> None:
    """An insert changes what the table says, so nothing weaker than `can_write_data` may record it."""
    assert relations_for_operation("insert") == ("can_write_data",)
    assert relations_for_operation(None) == ("can_write_data",), "an operation-less event must not fall to the weaker rung"
    assert relations_for_operation("drop_table") == ("can_write_data",)


def test_a_MAINTENANCE_operation_accepts_either_rung() -> None:
    """`model.fga` separates maintainer from writer in BOTH directions, and the owner ruling
    (2026-09-08, zero trust) gives the sweep the maintainer rung and refuses it the writer one — so
    authorizing a compaction on `can_write_data` alone would demand a grant the estate will not issue.

    EITHER, never `can_maintain` alone: `create_index` is emitted by the catalog's own door as well as
    by the sweep, and that door gates it at the writer tier, so accepting only the maintainer rung would
    refuse the human who actually built the index. Adding the path takes nothing from a writer.
    """
    for operation in ("compaction", "create_index"):
        assert relations_for_operation(operation) == ("can_write_data", "can_maintain"), operation


def test_a_REFUSED_event_is_DROPPED_never_retried() -> None:
    """Redelivery cannot grant a permission. A refused event that RETRIES burns the sidecar's delivery
    budget and then parks a permanent refusal on the dead-letter topic dressed as an outage, where an
    operator reads it as infrastructure rather than policy."""
    repo = _Repo()

    async def deny(_: RunEvent) -> None:
        raise PermissionDeniedError("can_write_data required on outputs: bronze$pages")

    status = asyncio.run(handle_cloud_event(cast(Any, repo), {"data": _event().model_dump(by_alias=True)}, deny))
    assert status == {"status": "DROP"}, f"a refused event asked the sidecar to redeliver: {status}"
    assert repo.ingested == [], "a refused event reached the graph"


def test_an_AUTHZ_OUTAGE_retries_and_never_drops() -> None:
    """An unreachable authorization service is an outage, not a verdict. Dropping on one deletes
    provenance for the length of the outage — silently, since a dropped event is acked — which is the
    exact failure this whole durable lane exists to prevent."""
    repo = _Repo()

    async def unavailable(_: RunEvent) -> None:
        raise RuntimeError("authorization service is not available")

    status = asyncio.run(handle_cloud_event(cast(Any, repo), {"data": _event().model_dump(by_alias=True)}, unavailable))
    assert status == {"status": "RETRY"}, f"an authz outage dropped provenance: {status}"
    assert repo.ingested == []


def test_an_AUTHORIZED_event_still_lands() -> None:
    """The guard is worthless if all it proves is that a raising callback raises."""
    repo = _Repo()

    async def allow(_: RunEvent) -> None:
        return None

    status = asyncio.run(handle_cloud_event(cast(Any, repo), {"data": _event().model_dump(by_alias=True)}, allow))
    assert status == {"status": "SUCCESS"}
    assert len(repo.ingested) == 1


def test_the_hook_is_OPTIONAL_so_an_unguarded_caller_is_unchanged() -> None:
    """`handle_cloud_event` is called without an authorizer by tests and by any deployment that has not
    wired one; that path must behave exactly as it did."""
    repo = _Repo()
    status = asyncio.run(handle_cloud_event(cast(Any, repo), {"data": _event().model_dump(by_alias=True)}))
    assert status == {"status": "SUCCESS"}
    assert len(repo.ingested) == 1


def test_an_UNAUTHORED_bus_event_is_refused() -> None:
    """The stamp is what the gate authorizes, so an event carrying none has nothing to authorize and
    must not fall through. Anonymous-beats-misattributed governs what gets RECORDED; it does not license
    an unauthenticated write."""
    from lineage.api import fga_deps

    class _Settings:
        fga_enabled = True
        fga_object_type = "table"

    class _App:
        state = type("S", (), {"fga": object(), "repository": None})()

    class _Request:
        app = _App()

    with pytest.raises(PermissionDeniedError, match="author"):
        asyncio.run(fga_deps.enforce_bus_authz(_event(), cast(Any, _Request()), cast(Any, _Settings())))


def test_the_ROUTE_itself_refuses_an_unauthorized_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTRACT, and the only tier that can prove it: driven through the REGISTERED route with the
    production wiring, so it fails if `register_dapr` stops passing the authorizer.

    The handler-level tests above take the callback as an argument and therefore cannot tell a wired
    door from an unwired one — which is exactly how this defect survived: `enforce_output_authz`'s own
    source named the asymmetry in a comment, and nothing executed against the door.
    """
    import logging

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from service_kit.governed import fga
    from service_kit.lakehouse.ns_errors import install_problem_handlers

    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    monkeypatch.setenv("LINEAGE_DAPR_ENABLED", "true")
    monkeypatch.setenv("RASK_FGA_ENABLED", "true")
    # FGA-on requires OIDC-on (`GovernedAuthSettings` refuses the half-governed shape), and OIDC-on
    # requires an issuer + audience. Supplied so this test exercises ITS subject rather than re-testing
    # that guard, which `test_the_chart_REFUSES...` already owns.
    monkeypatch.setenv("RASK_OIDC_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ISSUER", "https://idp.invalid/dex")
    monkeypatch.setenv("RASK_OIDC_AUDIENCE", "rask")

    from lineage.api.dapr import register_dapr
    from lineage.core.config import get_settings

    async def deny_everything(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        del user, relation
        return dict.fromkeys(objects, False)

    monkeypatch.setattr(fga, "batch_check", deny_everything)

    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    register_dapr(app)
    get_settings.cache_clear()

    repo = _RepoWithOutputs()
    app.state.repository = repo
    app.state.fga = object()

    payload = {"data": _event(author={"name": "mallory", "sub": "mallory"}).model_dump(by_alias=True)}
    response = TestClient(app).post("/lineage-events", json=payload, headers={"dapr-api-token": "s3cret"})

    assert response.status_code == 200, "a refusal must be an ACK, not an error the sidecar retries"
    assert response.json() == {"status": "DROP"}, f"the door recorded provenance it never authorized: {response.json()}"
    assert repo.ingested == [], "an unauthorized delivery reached the graph"


# --------------------------------------------------------------------------- #
# The replay exemption — measured on the estate before it was written
# --------------------------------------------------------------------------- #


class _Settings:
    fga_enabled = True
    fga_object_type = "table"


def _request(repository: object) -> Any:
    class _App:
        state = type("S", (), {"fga": object(), "repository": repository})()

    return type("R", (), {"app": _App()})()


class _Feed:
    """A repository double holding exactly one recorded event."""

    def __init__(self, stored: dict[str, Any] | None) -> None:
        self._stored = stored

    async def recorded_event(self, _run_id: str, _event_type: str | None) -> dict[str, Any] | None:
        return self._stored

    async def run_output_names(self, _run_id: str) -> list[str]:
        return []


def _deny_all(monkeypatch: pytest.MonkeyPatch) -> None:
    from service_kit.governed import fga

    async def deny(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        del user, relation
        return dict.fromkeys(objects, False)

    monkeypatch.setattr(fga, "batch_check", deny)


def test_a_BYTE_IDENTICAL_redelivery_is_not_a_new_assertion(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lineage restart re-presents the ENTIRE retained stream — the consumer is ephemeral with
    `deliverPolicy: all`, which is the estate's own recovery story. Measured 2026-09-09: a restart
    replayed all 2 161 retained messages and left the durable feed flat at 3 248 rows, because every one
    was already recorded. Refusing those loses nothing and turns each restart into a burst of refusals
    that reads exactly like a producer under attack.
    """
    from lineage.api import fga_deps

    _deny_all(monkeypatch)
    event = _event(author={"name": "someone", "sub": "someone"})
    stored = event.model_dump(by_alias=True)
    asyncio.run(fga_deps.enforce_bus_authz(event, _request(_Feed(stored)), cast(Any, _Settings())))


def test_a_replay_that_DIFFERS_IN_ANY_FIELD_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE HOLE THIS AVOIDS, and it is the reason the check is byte-identical rather than a key match.

    The graph MERGEs on run id and SETs `author`, `operation` and `event_type` last-wins, so exempting
    anything whose `(run_id, event_type)` merely EXISTS would let a forger take a known run id, restamp
    it with their own author — or as a `drop_table` the reconcile sweep then honours — and have the gate
    wave it through as a redelivery.
    """
    from lineage.api import fga_deps

    _deny_all(monkeypatch)
    event = _event(author={"name": "mallory", "sub": "mallory"})
    stored = _event(author={"name": "the-real-author", "sub": "the-real-author"}).model_dump(by_alias=True)
    with pytest.raises(PermissionDeniedError):
        asyncio.run(fga_deps.enforce_bus_authz(event, _request(_Feed(stored)), cast(Any, _Settings())))


def test_an_event_the_feed_has_NEVER_seen_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exemption must not become the rule: a first delivery has nothing to compare against."""
    from lineage.api import fga_deps

    _deny_all(monkeypatch)
    with pytest.raises(PermissionDeniedError):
        asyncio.run(fga_deps.enforce_bus_authz(_event(author={"name": "x", "sub": "x"}), _request(_Feed(None)), cast(Any, _Settings())))


def test_an_UNAUTHORED_replay_is_still_exempt_but_an_unauthored_NEW_event_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unauthored refusal takes the same path, and must: the 518 unauthored compactions in the
    retained stream predate the producer fix and replay on every restart. An unauthored event the feed
    has never seen is a new assertion with nothing to authorize, and stays refused."""
    from lineage.api import fga_deps

    _deny_all(monkeypatch)
    unauthored = _event()
    asyncio.run(fga_deps.enforce_bus_authz(unauthored, _request(_Feed(unauthored.model_dump(by_alias=True))), cast(Any, _Settings())))
    with pytest.raises(PermissionDeniedError, match="author"):
        asyncio.run(fga_deps.enforce_bus_authz(unauthored, _request(_Feed(None)), cast(Any, _Settings())))
