"""The bus door authorizes the stamped subject, as the HTTP door does (§ E2).

THE ASYMMETRY THIS CLOSES. `endpoints/ingest.py` applies `enforce_author` (bind the run to the verified
principal) and `enforce_output_authz` (may that principal write these outputs). A delivery off the bus has no
principal: the bus's permission table authenticates a TRANSPORT and not a producer, and the author is read off the
payload. Without the gate, any producer the bus lets publish could record any provenance about any dataset — including
an `operation: drop_table` on a table it had never seen.

THE ORDERING RULE THIS ROW SET, and why the gate is only now safe: "a consumer-side gate is only safe
once the producers sign". Measured 2026-09-08, 664 of 5 644 runs carried no author and 518 of those were
the maintenance sweep's, so this gate would have silently deleted the whole maintenance plane's
provenance. Re-measured 2026-09-09 after the producer half landed: every maintenance run since the roll
carries `service-maintenance`, and the only unauthored runs left were e2e fixture rows written straight
to the store, which never pass through this door.

Every check is the real model's on an OpenFGA store of the test's own, where bob owns the tables the events name, so
each is governed and a refusal of anyone else is a denial rather than a table with nothing to grant on.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from lance_namespace import PermissionDeniedError
from lineage_lane import Feed, governed, started

from lakehouse.events.bus_delivery import Outcome, Settlement
from lakehouse.events.lineage_events import DatasetUuids, with_dataset_uuids
from lakehouse.governed import fga_provision
from lakehouse.governed.audit import AUDIT_LOGGER
from lakehouse.governed.decider import OpenFgaDecider
from lakehouse.governed.fga_ids import user_object
from lineage.api import fga_deps
from lineage.api.fga_deps import relations_for_operation
from lineage.core.config import LineageSettings, Signing
from lineage.models import RunEvent


#: The uuids the catalog minted for the tables the events below name, which it stamps on every event it emits.
_PAGES, _SOURCE = "0b5f8f6c-3d0a-4c39-9a51-8f0b6d1a2e01", "7c2e1d4b-5a6f-4b8e-8c1d-2f3a4b5c6d02"


@pytest.fixture
def store(openfga_store: Any) -> Any:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """A store of the test's own on which bob owns both tables, so each is governed."""
    openfga_store.write(*((user_object("dex~bob"), "owner", f"table:{uuid}") for uuid in (_PAGES, _SOURCE)))
    return openfga_store


def _dataset(name: str, uuid: str) -> dict[str, Any]:
    return with_dataset_uuids({"namespace": "bronze", "name": name}, DatasetUuids(table_uuid=uuid), producer="test")


def _event(*, inputs: list[dict[str, Any]] | None = None, **run_facets: Any) -> RunEvent:
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-09T00:00:00+00:00",
            "run": {"runId": "11111111-1111-1111-1111-111111111111", "facets": {**run_facets, "rask_lance": {"operation": "insert"}}},
            "job": {"namespace": "bus", "name": "probe"},
            "inputs": inputs or [],
            "outputs": [_dataset("bronze$pages", _PAGES)],
        }
    )


def test_a_DATA_write_demands_the_writer_rung() -> None:
    """An insert changes what the table says, so nothing weaker than `can_write_data` may record it."""
    assert relations_for_operation("insert") == ("can_write_data",)
    assert relations_for_operation(None) == ("can_write_data",), "an operation-less event must not fall to the weaker rung"
    assert relations_for_operation("drop_table") == ("can_write_data",)


def test_the_lane_parks_an_unauthorized_delivery_unrecorded(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, store: Any) -> None:  # noqa: ANN401
    """CONTRACT, and the only tier that can prove it: driven through lineage's own lane with the production wiring, so
    it fails if the lane stops passing the authorizer.

    The gate-level tests below take the gate as a function and therefore cannot tell a wired door from an unwired one —
    which is how this defect survived: `enforce_output_authz`'s own source named the asymmetry in a comment, and nothing
    executed against the door. A named person lacking a grant is parked for the estate admin's replay, never recorded.

    Who was refused is on the audit trail, and the gate's own log line of the refusal names no subject: the service's
    log is not where a person's identity is kept.
    """
    governed(monkeypatch, store)
    envelope = {"id": "evt-mallory", "data": _event(rask_author={"name": "mallory", "sub": "dex~mallory"}).model_dump(by_alias=True)}

    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    caplog.set_level(logging.INFO, logger=fga_deps.log.name)
    with started(monkeypatch, Feed()) as lineage:
        settled = lineage.deliver(envelope)

    assert settled == Settlement(outcome=Outcome.PARKED)
    assert lineage.feed.recorded == [], "an unauthorized delivery reached the feed"
    assert list(lineage.feed.kept) == ["dead_letter:evt-mallory"]
    refusals = [r.__dict__ for r in caplog.records if r.name == AUDIT_LOGGER and r.__dict__["audit.outcome"] == "deny"]
    assert [(r["audit.action"], r["audit.subject"], r["audit.relation"], r["audit.resource"]) for r in refusals] == [
        ("authz", "dex~mallory", "can_write_data", f"table:{_PAGES}")
    ]
    [said] = [r.__dict__ for r in caplog.records if r.name == fga_deps.log.name and r.getMessage() == "ingest_denied"]
    assert (said["relation"], said["objects"], "dex~mallory" in repr(said)) == ("can_write_data", [f"table:{_PAGES}"], False)


# --------------------------------------------------------------------------- #
# The replay exemption — measured on the estate before it was written
# --------------------------------------------------------------------------- #


def _settings() -> LineageSettings:
    """Authorization on, and no signer listed, so only the grants decide."""
    return LineageSettings.model_validate(
        {
            "oidc_enabled": True,
            "oidc_issuer": "https://idp.invalid/dex",
            "oidc_audience": "rask",
            "oidc_idp_id": "dex",
            "oidc_connectors": {"local": "dex"},
            "fga_enabled": True,
            "fga_store_id": "s",
            "signing": Signing(signers=frozenset(), delegators=frozenset()),
        }
    )


def _gate(store: Any, event: RunEvent, stored: dict[str, Any] | None, delivered: dict[str, Any]) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """Run the bus door's gate over ``event`` as ``delivered``, the feed holding ``stored``, every check asked of ``store``."""
    from lineage.api import fga_deps

    async def gate() -> None:
        async with fga_provision.make_client(store.url, store.store_id, store.model_id, token_file=None) as client:
            app = FastAPI()
            app.state.decider = OpenFgaDecider(client)
            app.state.provenance = _Feed(stored)
            await fga_deps.enforce_bus_authz(event, app, _settings(), arrived=delivered)

    asyncio.run(gate())


class _Feed:
    """A store double holding exactly one recorded event."""

    def __init__(self, stored: dict[str, Any] | None) -> None:
        self._stored = stored

    async def recorded_event(self, _key: str) -> dict[str, Any] | None:
        return self._stored

    async def run_author(self, _run_id: str) -> str | None:
        return None


def test_a_BYTE_IDENTICAL_redelivery_is_not_a_new_assertion(store: Any) -> None:  # noqa: ANN401
    """A lineage restart re-presents the ENTIRE retained stream — the consumer is ephemeral with
    `deliverPolicy: all`, which is the estate's own recovery story. Measured 2026-09-09: a restart
    replayed all 2 161 retained messages and left the durable feed flat at 3 248 rows, because every one
    was already recorded. Refusing those loses nothing and turns each restart into a burst of refusals
    that reads exactly like a producer under attack.
    """
    event = _event(rask_author={"name": "someone", "sub": "dex~someone"})
    stored = event.model_dump(by_alias=True)
    _gate(store, event, stored, event.model_dump(by_alias=True))


def test_a_replay_that_DIFFERS_IN_ANY_FIELD_is_still_refused(store: Any) -> None:  # noqa: ANN401
    """THE HOLE THIS AVOIDS, and it is the reason the check is byte-identical rather than a key match.

    Exempting anything whose `(run_id, event_type)` merely EXISTS would let a forger take a known run id,
    restamp it with their own author — or as a `drop_table` — and have the gate wave it through as a
    redelivery on the strength of the honest row beside it.
    """
    event = _event(rask_author={"name": "mallory", "sub": "dex~mallory"})
    stored = _event(rask_author={"name": "the-real-author", "sub": "dex~the-real-author"}).model_dump(by_alias=True)
    with pytest.raises(PermissionDeniedError, match="can_write_data"):
        _gate(store, event, stored, event.model_dump(by_alias=True))


def test_an_UNAUTHORED_replay_is_still_exempt_but_an_unauthored_NEW_event_is_not(store: Any) -> None:  # noqa: ANN401
    """The unauthored refusal takes the same path, and must: the 518 unauthored compactions in the
    retained stream predate the producer fix and replay on every restart. An unauthored event the feed
    has never seen is a new assertion with nothing to authorize, and stays refused."""
    unauthored = _event()
    _gate(store, unauthored, unauthored.model_dump(by_alias=True), unauthored.model_dump(by_alias=True))
    with pytest.raises(PermissionDeniedError, match="author"):
        _gate(store, unauthored, None, {})


def test_a_write_naming_a_table_by_a_name_another_rename_took_is_authorized_on_the_uuid_it_carries(store: Any) -> None:  # noqa: ANN401
    """A rename keeps a table's uuid, and its grants with it (ADR 0173 §8, [[LH-524]]): an event the catalog emitted
    under a name the table has since given up still carries the uuid, and the door asks about that uuid, never the name.
    Measured on LH-522's read-back (rev 1), the door refused such an event `can_get_metadata required on inputs: <ns>$t`."""
    event = _event(inputs=[_dataset("bronze$renamed_since", _SOURCE)], rask_author={"name": "pipeline", "sub": "dex~pipeline"})
    store.write((user_object("dex~pipeline"), "writer", f"table:{_PAGES}"))
    with pytest.raises(PermissionDeniedError, match=r"can_read_data required on inputs: bronze\$renamed_since"):
        _gate(store, event, None, event.model_dump(by_alias=True))

    store.write((user_object("dex~pipeline"), "reader", f"table:{_SOURCE}"))

    _gate(store, event, None, event.model_dump(by_alias=True))
