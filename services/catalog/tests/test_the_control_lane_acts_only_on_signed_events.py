"""[[CTL-32]] Each catalog replica judges every control event it follows, and counts what no control signer signed (ADR 0171).

Every user the bus lets publish on `catalog.control.v1` reaches the stream, so the actor an event names is a claim until
its signature proves it. The console's feed serves only what the enforcing door admits
(`test_any_replica_answers_the_events_poll.py`); the lane is the alarm: each replica judges each event it follows and
counts a refusal in `catalog.signature.refused`, which pages whether or not a console polls. A message that is no
control event is passed over. An event whose signers' keys cannot be read is judged again a bounded number of times,
then given up as not handled; and the lane waits that way once in a while, not for every event, since a wait holds back
every event behind it.

Driven through the catalog's own handling of one followed event (`catalog.api.control_lane.ControlLane.handle`) on the
app's loop. The keys are read from the secret store, which the suite's double answers.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from catalog.api import control_lane
from catalog.api.control_lane import TOPIC, ControlLane, KeysUnavailableError
from catalog.api.control_relay import attach_published_keys
from catalog.core.config import Settings, get_settings
from lakehouse.bus_delivery import Delivery
from lakehouse.governed.signing_key import signing_public_secret
from tests.secret_store_double import SecretStoreDouble


CATALOG = "kubernetes~system:serviceaccount:default:rask-sa-catalog"
MAINTENANCE = "kubernetes~system:serviceaccount:default:rask-sa-maintenance"

#: Deletes warehouse `wh-acme`: fed to the console, it tells every reader the warehouse is gone.
FORGED_DELETE: dict[str, Any] = {
    "event_id": "forged-delete",
    "occurred_at": "2026-10-18T12:00:00+00:00",
    "action": "warehouse_deleted",
    "object_type": "warehouse",
    "object_id": "warehouse:wh-acme",
    "actor": "user:dex~mallory",
    "extra": {"namespaces_dropped": ["wh-acme$sales"], "project": "acme"},
}

#: What the catalog itself announced: `beta` was dropped, named by its uuid and its name (ADR 0173 §8).
GENUINE_DROP: dict[str, Any] = {
    "event_id": "genuine-drop",
    "occurred_at": "2026-10-18T12:00:01+00:00",
    "action": "namespace_dropped",
    "object_type": "namespace",
    "object_id": "namespace:7d2e4c19-8a3b-4e6f-b1d0-5c9a2e8f3b47",
    "object_name": "wh-beta$sales",
    "actor": "user:dex~alice",
    "extra": {},
}


class _Lane:
    """A catalog app's lane, both signers, and what the lane counts."""

    def __init__(self, client: TestClient, lane: ControlLane, catalog: Any, maintenance: Any, reader: InMemoryMetricReader) -> None:
        self.client = client
        self.catalog = catalog
        self.maintenance = maintenance
        self._lane = lane
        self._reader = reader
        self._handed = 0

    def hand(self, event: dict[str, Any]) -> None:
        """Hand ``event`` to the lane on the app's loop, in the envelope the bus carries it in."""
        self._handed += 1
        delivery = Delivery(subject=TOPIC, body=json.dumps({"data": event}).encode(), sequence=self._handed, attempt=1)
        portal = self.client.portal
        assert portal is not None, "the client was never entered, so the app has no loop to hand the event on"
        portal.call(self._lane.handle, delivery)

    def refused(self) -> dict[str, int]:
        """`catalog.signature.refused` by reason, as the lane counted it."""
        counted: dict[str, int] = {}
        data = self._reader.get_metrics_data()
        for resource in getattr(data, "resource_metrics", []) or []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == "catalog.signature.refused":
                        for point in metric.data.data_points:
                            if point.value:
                                counted[str(point.attributes["lance.catalog.reason"])] = int(point.value)
        return counted


def _unsigned(_catalog: Any, _maintenance: Any) -> dict[str, Any]:
    return FORGED_DELETE


def _signed_by_a_signer_whose_role_is_not_this_actions(_catalog: Any, maintenance: Any) -> dict[str, Any]:
    """Maintenance announces the delete as its own act: an event that names no person needs no delegation, so the role
    its action is signed under is the one rule that refuses it."""
    return maintenance.sign_control({**FORGED_DELETE, "actor": None})


@pytest.fixture
def lane(monkeypatch: pytest.MonkeyPatch, event_signer: Any, secret_store: SecretStoreDouble, respx_allows_unused_routes: None) -> Iterator[_Lane]:
    """A catalog app carrying the lane, both signers, each one's key list held by the store, and a reader of what the lane
    counts."""
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("LANCE_CONTROL_EMIT_ENABLED", "true")
    # The bus the chart names a catalog that emits; nothing here publishes on it.
    monkeypatch.setenv("RASK_BUS_URL", "nats://bus.test:4222")
    monkeypatch.setenv("RASK_BUS_USER", "catalog")
    get_settings.cache_clear()
    catalog, maintenance = event_signer(CATALOG), event_signer(MAINTENANCE)
    settings = Settings.model_validate(
        {
            "LANCE_S3_ACCESS_KEY_ID": "k",
            "LANCE_S3_SECRET_ACCESS_KEY": "s",
            "LANCE_CONTROL_EMIT_ENABLED": "true",
            "RASK_SIGNATURE_DOORS": "enforce",
            "RASK_EVENT_SIGNERS": [CATALOG, MAINTENANCE],
            "RASK_EVENT_DELEGATORS": [CATALOG],
            "RASK_CONTROL_SIGNER_ROLES": {"catalog": [CATALOG], "maintenance": [MAINTENANCE]},
        }
    )
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("lance.catalog")
    monkeypatch.setattr(control_lane, "_signature_refused", meter.create_counter("catalog.signature.refused"))
    app = FastAPI()
    app.dependency_overrides[get_settings] = lambda: settings
    attach_published_keys(app)
    for signer in (catalog, maintenance):
        secret_store.hold(signing_public_secret(signer.identity), {"keys": signer.public})
    try:
        with TestClient(app) as client:
            yield _Lane(client, ControlLane(app, settings), catalog, maintenance, reader)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("forge", "reason"),
    [
        pytest.param(_unsigned, "unsigned", id="unsigned"),
        pytest.param(_signed_by_a_signer_whose_role_is_not_this_actions, "signer", id="signed-by-another-role"),
    ],
)
def test_a_control_event_no_signer_of_its_action_signed_is_counted_and_a_signed_one_is_not(
    lane: _Lane, forge: Callable[[Any, Any], dict[str, Any]], reason: str
) -> None:
    lane.hand(forge(lane.catalog, lane.maintenance))
    lane.hand(lane.catalog.sign_control(GENUINE_DROP, on_behalf_of=GENUINE_DROP["actor"]))

    assert lane.refused() == {reason: 1}


def test_a_message_that_is_no_control_event_is_passed_over(lane: _Lane) -> None:
    """A follower is handed each message once, so one it cannot read must not stop the events behind it."""
    lane.hand({"event_id": "not-an-event", "action": "no_such_action"})
    lane.hand(lane.catalog.sign_control(GENUINE_DROP, on_behalf_of=GENUINE_DROP["actor"]))

    assert lane.refused() == {}


@pytest.fixture
def keys_unread_for(lane: _Lane, secret_store: SecretStoreDouble, monkeypatch: pytest.MonkeyPatch) -> Callable[[int], respx.Route]:
    """`keys_unread_for(n)`: the catalog's key list answers 503 to its first ``n`` reads and its keys after; the lane's
    wait between reads shrunk to nothing."""
    monkeypatch.setattr(control_lane, "_KEY_READ_SECONDS", 0.0)

    def _unread(reads: int) -> respx.Route:
        keys = secret_store.hold(signing_public_secret(CATALOG), httpx.Response(503))
        keys.side_effect = [httpx.Response(503)] * reads + [SecretStoreDouble.held({"keys": lane.catalog.public})]
        return keys

    return _unread


@pytest.mark.parametrize(
    ("forge", "reason"),
    [
        pytest.param(_unsigned, "unsigned", id="unsigned"),
        pytest.param(_signed_by_a_signer_whose_role_is_not_this_actions, "signer", id="signed-by-another-role"),
    ],
)
def test_an_event_whose_keys_are_read_on_the_lanes_last_try_is_judged_and_counted(
    lane: _Lane, keys_unread_for: Callable[[int], respx.Route], forge: Callable[[Any, Any], dict[str, Any]], reason: str
) -> None:
    """A store that is away says nothing about the signature, so the event is judged again until its keys are read: a
    made-up key id is refused only on a read of the list after the one that did not hold it."""
    keys_unread_for(control_lane._KEY_READS - 1)

    lane.hand(lane.catalog.sign_control(GENUINE_DROP, on_behalf_of=GENUINE_DROP["actor"]))
    lane.hand(forge(lane.catalog, lane.maintenance))

    assert lane.refused() == {reason: 1}


def test_an_event_whose_keys_are_never_read_is_given_up_uncounted(lane: _Lane, keys_unread_for: Callable[[int], respx.Route]) -> None:
    """Past the lane's bound the event is given up as not handled: neither counted refused nor passed as signed."""
    keys = keys_unread_for(control_lane._KEY_READS)

    with pytest.raises(KeysUnavailableError, match="could not be read on 5 tries"):
        lane.hand(lane.catalog.sign_control(GENUINE_DROP, on_behalf_of=GENUINE_DROP["actor"]))

    assert (lane.refused(), keys.call_count) == ({}, control_lane._KEY_READS)


@pytest.mark.parametrize(
    ("wait_again_seconds", "reads", "given_up"),
    [
        pytest.param(300.0, control_lane._KEY_READS + 1, "the lane waited for keys moments ago", id="moments-after-a-wait-it-is-judged-once"),
        pytest.param(0.0, 2 * control_lane._KEY_READS, "could not be read on 5 tries", id="once-the-time-has-passed-it-is-waited-for-again"),
    ],
)
def test_the_lane_waits_for_keys_once_in_a_while_not_for_every_event(  # noqa: PLR0913 — the case, and what drives and reads the lane
    lane: _Lane, keys_unread_for: Callable[[int], respx.Route], monkeypatch: pytest.MonkeyPatch, wait_again_seconds: float, reads: int, given_up: str
) -> None:
    """The follow hands on one event at a time, so a wait for keys holds back every event behind it: an event whose
    keys cannot be read moments after the lane waited is given up on its one judgement."""
    monkeypatch.setattr(control_lane, "_WAIT_AGAIN_SECONDS", wait_again_seconds)
    keys = keys_unread_for(reads)
    with pytest.raises(KeysUnavailableError):
        lane.hand(lane.catalog.sign_control(GENUINE_DROP, on_behalf_of=GENUINE_DROP["actor"]))

    with pytest.raises(KeysUnavailableError, match=given_up):
        lane.hand(lane.catalog.sign_control({**GENUINE_DROP, "event_id": "genuine-drop-behind"}, on_behalf_of=GENUINE_DROP["actor"]))

    assert (lane.refused(), keys.call_count) == ({}, reads)
