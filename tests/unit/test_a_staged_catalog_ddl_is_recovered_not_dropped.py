"""A catalog DDL event staged on a failed publish must be recovered by the relay, not deleted as poison.

[[LH-199]] A create, drop, deregister, alter, declare or register reaches the bus as an OpenLineage
`DatasetEvent` (`build_write_event` -> `_as_dataset_event`). The drain and both DLQ routes parsed staged
bytes as `RunEvent` only, so the staged copy of every catalog DDL announcement failed on
`eventType`/`run`/`job`, was logged `lineage_outbox_poison_dropped` and deleted — on exactly the bus
outage the outbox exists for. Measured on ef9d1e8e with the real emitter and the real drain:
`drained=0`, zero ingests, the object gone.

Staged the way the catalog stages it: its real builder's DatasetEvent, signed as the catalog on behalf of the person it
authenticated, left in the outbox when the publish fails, and read back through the doors' own discriminator, so the producer
and the relay cannot disagree unseen. The relay verifies that signature against the key the catalog publishes (served here by
the suite's secret-store double), so every test below reaches its assertion through a verified event; the signature is made by the
root conftest's `EventSigner`, which writes the wire format from the contract alone. Where OpenFGA is consulted it is the pinned
server, on a store of the test's own holding the tuples a case names.
"""

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from typing import Any, Final, cast, override

import pytest
from fastapi import Request
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, TransactionNotFoundError, UnsupportedOperationError
from pydantic import SecretStr

from catalog.core.lineage_emit import INSERT, build_write_event
from lakehouse.events import outbox, outbox_metrics
from lakehouse.events.bus import BusUnavailableError
from lakehouse.events.cloud_events import lineage_envelope
from lakehouse.events.lineage_events import DatasetUuids
from lakehouse.events.openlineage import run_id_for
from lakehouse.events.signing import signature_of
from lakehouse.governed import fga_ids, fga_provision
from lakehouse.governed.audit import AUDIT_LOGGER
from lakehouse.governed.decider import OpenFgaDecider
from lakehouse.storage import store_session
from lakehouse.storage.credential import S3KeyPair
from lakehouse.storage.options import StorageOptions
from lineage.api import fga_deps, relay
from lineage.api.v1.endpoints import dlq
from lineage.core.config import LineageSettings, Signing
from lineage.core.refusals import UnverifiedEventError
from lineage.models import DatasetEvent, RunEvent
from lineage.services.provenance import ProvenanceStore, feed_key
from lineage.services.signature import store_published_keys
from tests.app_state import request_on
from tests.bus_double import BusDouble
from tests.callers import person
from tests.provenance_store_double import ProvenanceStoreDouble
from tests.secret_store_double import SecretStoreDouble


_CATALOG = "service-catalog"
_AUTHOR = "dex~alice"
_TABLE = "gov$raw"


def _uuids(table: str) -> DatasetUuids:
    """The uuid the catalog minted for ``table``, which its events carry (ADR 0173 §10) and lineage checks ([[LH-524]])."""
    return DatasetUuids(table_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, table)))


#: Bytes that are valid JSON text and no envelope: an integer past what the parser holds. Any stager holding the outbox
#: credential can write them.
_HOSTILE: Final = frozendict({"bigint": '{"eventType": ' + "1" * 5000 + "}"})


def _datasets(store: ProvenanceStoreDouble) -> list[DatasetEvent]:
    """What ``store`` recorded as a dataset event, the shape a staged DDL is parsed into."""
    return [event for event in store.recorded if isinstance(event, DatasetEvent)]


def _runs(store: ProvenanceStoreDouble) -> list[RunEvent]:
    """What ``store`` recorded as a run event."""
    return [event for event in store.recorded if isinstance(event, RunEvent)]


@pytest.fixture
def catalog(event_signer: Any, secret_store: SecretStoreDouble, respx_allows_unused_routes: None) -> Any:  # noqa: ANN401 — the root conftest's `EventSigner`
    """The catalog's signing identity, its public key held by the secret store. A test whose event is refused or poison
    before its signature is judged never reads that key."""
    signer = event_signer(_CATALOG)
    secret_store.hold(f"signing-public-{_CATALOG}", {"keys": signer.public})
    return signer


@pytest.fixture(autouse=True)
def _a_session_cache_of_the_tests_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store session `_settings` holds for lineage ends with the test that asked for it, and no key pair another test
    left in this process's environment stands beside lineage's token."""
    monkeypatch.setattr(store_session, "SESSIONS", store_session.SessionCache())
    monkeypatch.delenv("LINEAGE_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("LINEAGE_S3_SECRET_ACCESS_KEY", raising=False)


def _settings(tmp_path: Path, *, fga_enabled: bool, signers: frozenset[str] = frozenset({_CATALOG})) -> LineageSettings:
    """Signing enforced, the catalog the one delegator and `signers` the identities that may sign, as the chart renders it,
    and lineage's store identity its projected token with the session it was exchanged for held, as the deployed
    service's is (ADR 0170): a process with a secret store is handed no key pair."""
    token = tmp_path / "rask-object-store-token"
    token.write_text("projected\n")
    auth = {
        "oidc_enabled": True,
        "oidc_issuer": "https://dex.example",
        "oidc_audience": "lance",
        "oidc_idp_id": "dex",
        "oidc_connectors": {"local": "dex"},
        "fga_store_id": "s",
    }
    settings = LineageSettings.model_validate(
        {
            "outbox_uri": str(tmp_path / "outbox"),
            "fga_enabled": fga_enabled,
            "signing": Signing(event_signers=signers, event_delegators=frozenset({_CATALOG})),
            "s3_web_identity_token_file": str(token),
            "s3_sts_endpoint": "http://object-store:9000",
            **(auth if fga_enabled else {}),
        }
    )
    identity = settings.web_identity
    assert identity is not None, "lineage holds no web identity, so it would sign with no session"
    session = S3KeyPair(access_key_id="LINEAGESESSION", secret_access_key=SecretStr("test-only"), session_token=SecretStr("test-only"))
    store_session.SESSIONS.hold(identity, session, asked_at=store_session.SESSIONS.now(), lifetime_seconds=3600)
    return settings


def _create_event(signer: Any, *, author: str = _AUTHOR) -> dict[str, Any]:  # noqa: ANN401 - the conftest's EventSigner
    """A create for `_TABLE` from the catalog's real builder, signed by `signer`: the catalog signs on behalf of `author`,
    any other signer for itself."""
    event = build_write_event(
        table_id=_TABLE,
        uuids=_uuids(_TABLE),
        namespace="lance",
        author=author,
        version=1,
        operation="create_table",
        run_id="0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a60",
        event_time="2026-10-02T12:00:00+00:00",
        job_namespace="lance",
    )
    return signer.sign(event, on_behalf_of=author) if signer.identity == _CATALOG else signer.sign(event)


def _stage_a_create(settings: LineageSettings, signer: Any, *, author: str = _AUTHOR) -> tuple[str, str]:  # noqa: ANN401 - the conftest's EventSigner
    """What a stager leaves in the outbox when its publish fails, and the create event it holds, signed."""
    doc = _create_event(signer, author=author)
    outbox.stage_envelope(settings.outbox_uri, StorageOptions(), lineage_envelope(doc))
    staged = list(outbox.list_events(settings.outbox_uri, StorageOptions()))
    assert len(staged) == 1, f"staging left {len(staged)} objects, not one"
    key, record = staged[0]
    event_json = json.dumps(json.loads(record)["data"])
    # ANTI-VACUITY: a run-shaped or unsigned fixture would pass every assertion below for the wrong reason.
    assert "dataset" in doc and "run" not in doc, "a create is no longer a DatasetEvent, so this file tests nothing"
    assert signature_of(doc) is not None, "the staged create is unsigned, so the signature leg of the gate never runs"
    return key, event_json


def _stage_hostile_first(settings: LineageSettings, body: str) -> str:
    """Stage bytes no parser accepts as the OLDEST object, so the drain meets them before anything else."""
    key = "hostile"
    path = Path(settings.outbox_uri) / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    os.utime(path, ns=(1, 1))
    return key


#: The object `_TABLE`'s grants are held by, the uuid its events carry.
_TABLE_OBJECT = f"table:{_uuids(_TABLE).table_uuid}"


def _arm_gate(store: Any, *, writer: str | None, readers: tuple[str, ...] = ("dex~curator",)) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """The real `enforce_bus_authz` on the test's own store: `writer` writes `_TABLE`, and each of `readers` reads it, so the
    table is governed and a denial is repairable rather than unrepairable."""
    store.write(
        *((fga_ids.user_object(reader), "reader", _TABLE_OBJECT) for reader in readers),
        *([(fga_ids.user_object(writer), "writer", _TABLE_OBJECT)] if writer else []),
    )


def _request(client: Any = None) -> Request:  # noqa: ANN401 — an OpenFGA client
    """The app state the drain and the routes read, its decider holding ``client``: one on the test's own store, or none
    for a case authorization never reaches."""
    return request_on(decider=OpenFgaDecider(cast("Any", client or object())), published_keys=store_published_keys())


def _decided[T](store: Any, call: Callable[[Any], Awaitable[T]]) -> T:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """Run ``call`` on a request whose decider asks the test's own OpenFGA store, on the loop its client is opened on."""

    async def _run() -> T:
        async with fga_provision.make_client(store.url, store.store_id, store.model_id, token_file=None) as client:
            return await call(_request(client))

    return asyncio.run(_run())


def _rewrite_staged(settings: LineageSettings, key: str, doc: dict[str, Any]) -> None:
    """Overwrite a staged object's event in place, as a stager holding the outbox credential can."""
    (Path(settings.outbox_uri) / f"{key}.json").write_text(json.dumps(lineage_envelope(doc).as_wire()))


def _without_signature(staged_json: str) -> dict[str, Any]:
    doc = json.loads(staged_json)
    doc["dataset"]["facets"].pop("rask_signature")
    assert signature_of(doc) is None
    return doc


def _drain(settings: LineageSettings, repo: ProvenanceStoreDouble, publisher: BusDouble, store: Any = None) -> relay.DrainOutcome:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """One drain of the outbox, its gate asking ``store`` where one is given."""

    async def _once(request: Any) -> relay.DrainOutcome:  # noqa: ANN401 — the stand-in request
        # The feed lineage's lifespan wires on its app, the one the relay hands the drain.
        request.app.state.provenance = repo
        return await relay._drain_outbox(request.app, cast(ProvenanceStore, repo), settings, opts=StorageOptions(), publisher=publisher)

    if store is None:
        return asyncio.run(_once(_request()))
    return _decided(store, _once)


def _stage_a_run_behind(settings: LineageSettings, create_key: str) -> str:
    """A run event staged AFTER the create, so the drain meets the create first and must still reach this."""
    event = build_write_event(
        table_id="raw$events",
        uuids=_uuids("raw$events"),
        namespace="raw",
        author=_AUTHOR,
        version=2,
        operation=INSERT,
        run_id=run_id_for("behind-the-create"),
        event_time="2026-10-12T00:00:00+00:00",
        job_namespace="lance-catalog",
    )
    run_id = event["run"]["runId"]
    outbox.stage_envelope(settings.outbox_uri, StorageOptions(), lineage_envelope(event))
    os.utime(Path(settings.outbox_uri) / f"{create_key}.json", ns=(1, 1))
    return run_id


async def _admit(_event: object, _app: object, _settings: object, *, arrived: object) -> frozenset[str]:
    """The gate admits: what is under test is the drain's handling after it, not the gate's decision."""
    return frozenset()


class _CreateDoorDown(ProvenanceStoreDouble):
    @override
    async def record(self, event: RunEvent | DatasetEvent, *, aliased: Collection[str]) -> None:
        if isinstance(event, DatasetEvent):
            raise ConnectionError("catalog unreachable")
        await super().record(event, aliased=aliased)


class _RefusalLedgerDown(ProvenanceStoreDouble):
    @override
    async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
        raise ConnectionError("catalog unreachable")


# --------------------------------------------------------------------------- #
# The relay
# --------------------------------------------------------------------------- #


def test_the_drain_re_ingests_a_staged_create_through_the_dataset_door(tmp_path: Path, openfga_store: Any, catalog: Any) -> None:  # noqa: ANN401
    settings = _settings(tmp_path, fga_enabled=True)
    _, staged_json = _stage_a_create(settings, catalog)
    _arm_gate(openfga_store, writer=_AUTHOR)
    repo, publisher = ProvenanceStoreDouble(), BusDouble()

    outcome = _drain(settings, repo, publisher, openfga_store)

    assert outcome.drained == 1, f"the staged create was not recovered: {outcome}"
    assert [e.dataset.name for e in _datasets(repo)] == [_TABLE] and _runs(repo) == [], "the create did not reach the dataset door"
    assert _datasets(repo)[0].operation == "create_table"
    assert publisher.events == [json.loads(staged_json)], "subscribers never hear of the create; the re-publish must carry the staged event"
    assert not list(outbox.list_events(settings.outbox_uri, StorageOptions())), "a recovered event must not stay staged"


def test_a_staged_create_the_gate_refuses_is_refused_and_never_ingested(tmp_path: Path, openfga_store: Any, catalog: Any) -> None:  # noqa: ANN401
    """The DatasetEvent path still runs `enforce_bus_authz`: a create no listed signer signed is refused, recorded and retired,
    never ingested and never announced, since a retry cannot change the answer."""
    settings = _settings(tmp_path, fga_enabled=True)
    key, staged_json = _stage_a_create(settings, catalog)
    staged_json = json.dumps(_without_signature(staged_json))
    _rewrite_staged(settings, key, json.loads(staged_json))
    _arm_gate(openfga_store, writer=_AUTHOR)
    repo, publisher = ProvenanceStoreDouble(), BusDouble()

    outcome = _drain(settings, repo, publisher, openfga_store)

    assert outcome.refused == outcome.recorded == 1 and outcome.drained == 0, f"a refused create was not handled as a refusal: {outcome}"
    assert repo.recorded == [] and publisher.events == [], "a refused create reached the feed or the bus"
    # No run to name: the key is what identifies a static change, and `run_id` stays empty rather than lying.
    [refusal] = repo.refusals
    assert "no signature" in str(refusal["reason"]), f"the recorded reason does not say what was refused: {refusal['reason']}"
    assert refusal == {"outbox_key": key, "run_id": None, "author": _AUTHOR, "reason": refusal["reason"], "event_json": staged_json}
    assert not list(outbox.list_events(settings.outbox_uri, StorageOptions())), "a recorded refusal must retire the staged object"


@pytest.mark.parametrize(
    ("by_the_catalog", "drained"),
    [
        pytest.param(True, 1, id="a-delegators-drop-is-admitted-though-its-grants-are-gone"),
        pytest.param(False, 0, id="another-listed-signers-drop-keeps-the-full-check"),
    ],
)
def test_the_relay_admits_a_delegators_drop_without_a_grant_and_no_other_signers(
    tmp_path: Path, openfga_store: Any, catalog: Any, event_signer: Any, secret_store: SecretStoreDouble, by_the_catalog: bool, drained: int
) -> None:
    """The catalog revokes a table's grants in the request that drops it, so a drop delivered after the revoke asks FGA about a
    table nobody may write. A drop whose signature verifies as a delegator is the catalog's attestation that it enforced `can_drop`,
    and is admitted without the grant; a drop any other listed signer signed still needs it."""
    other = event_signer("service-pipeline-a")
    secret_store.hold(f"signing-public-{other.identity}", {"keys": other.public})
    settings = _settings(tmp_path, fga_enabled=True, signers=frozenset({_CATALOG, other.identity}))
    author = _AUTHOR if by_the_catalog else other.identity
    event = build_write_event(
        table_id=_TABLE,
        uuids=_uuids(_TABLE),
        namespace="lance",
        author=author,
        version=None,
        operation="drop_table",
        run_id="0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a61",
        event_time="2026-10-02T12:00:00+00:00",
        job_namespace="lance",
    )
    signed = catalog.sign(event, on_behalf_of=author) if by_the_catalog else other.sign(event)
    outbox.stage_envelope(settings.outbox_uri, StorageOptions(), lineage_envelope(signed))
    _arm_gate(openfga_store, writer=None)
    repo = ProvenanceStoreDouble()

    outcome = _drain(settings, repo, BusDouble(), openfga_store)

    assert (outcome.drained, outcome.refused) == (drained, 1 - drained), f"the relay answered {outcome}"
    assert [(e.dataset.name, e.operation) for e in _datasets(repo)] == ([(_TABLE, "drop_table")] if drained else [])


@pytest.mark.parametrize("body", _HOSTILE.values(), ids=_HOSTILE.keys())
def test_bytes_no_parser_accepts_are_poison_and_do_not_wedge_the_create_behind_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openfga_store: Any, body: str, catalog: Any
) -> None:
    """Read first, such an object is dropped as poison; one escaping the parse would abort every tick."""
    settings = _settings(tmp_path, fga_enabled=True)
    _stage_a_create(settings, catalog)
    hostile = _stage_hostile_first(settings, body)
    _arm_gate(openfga_store, writer=_AUTHOR)
    poison: list[None] = []
    monkeypatch.setattr(outbox_metrics, "record_poison_dropped", lambda lane: poison.append(lane))
    repo = ProvenanceStoreDouble()

    outcome = _drain(settings, repo, BusDouble(), openfga_store)

    assert (outcome.drained, poison) == (1, ["lineage"]), f"the hostile object was not handled as poison of the lineage outbox: {outcome}, poison={poison}"
    assert [e.dataset.name for e in _datasets(repo)] == [_TABLE], "the create staged behind the hostile object was not recovered"
    assert outbox.resolve_event(settings.outbox_uri, StorageOptions(), hostile) is None, "the poison object stays staged to wedge the next tick"


def test_a_create_whose_ingest_fails_is_stranded_and_the_run_behind_it_still_drains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any) -> None:
    """PER-EVENT ISOLATION holds for the new event kind: a transient failure on a create strands that
    object and the tick goes on. The stranded log names `event.run_id`, which a `DatasetEvent` answers
    with `None`; `event.run.run_id` there would raise AttributeError out of the handler and abort the tick."""
    settings = _settings(tmp_path, fga_enabled=False)
    key, _ = _stage_a_create(settings, catalog)
    run_id = _stage_a_run_behind(settings, key)
    monkeypatch.setattr(relay, "enforce_bus_authz", _admit)
    repo = _CreateDoorDown()

    outcome = _drain(settings, repo, BusDouble())

    assert (outcome.drained, outcome.stranded, outcome.refused) == (1, 1, 0), f"one failed create aborted or misreported the tick: {outcome}"
    assert [event.run.run_id for event in _runs(repo)] == [run_id], "the run staged behind the failed create was not recovered"
    assert outbox.resolve_event(settings.outbox_uri, StorageOptions(), key) is not None, "a transient failure destroyed the create's only durable copy"


def _stage_a_refusable_first(settings: LineageSettings, kind: str, catalog: Any) -> tuple[str, str]:  # noqa: ANN401 - the conftest's EventSigner
    """The event the gate will refuse, staged oldest, and a run staged behind it. Returns (refused key, run id behind)."""
    if kind == "create":
        key, _ = _stage_a_create(settings, catalog)
        return key, _stage_a_run_behind(settings, key)
    event = build_write_event(
        table_id="raw$events",
        uuids=_uuids("raw$events"),
        namespace="raw",
        author=_AUTHOR,
        version=1,
        operation=INSERT,
        run_id=run_id_for("refused-run"),
        event_time="2026-10-12T00:00:00+00:00",
        job_namespace="lance-catalog",
    )
    outbox.stage_envelope(settings.outbox_uri, StorageOptions(), lineage_envelope(event))
    [(key, _)] = list(outbox.list_events(settings.outbox_uri, StorageOptions()))
    return key, _stage_a_run_behind(settings, key)


def _refusing(key: str, settings: LineageSettings) -> Any:  # noqa: ANN401 — the gate's own signature
    """A gate that refuses exactly the event staged under `key` and admits the rest."""
    refused_json = outbox.resolve_event(settings.outbox_uri, StorageOptions(), key)
    assert refused_json is not None
    refused_doc = json.loads(refused_json[1])["data"]

    async def gate(_event: object, _app: object, _settings: object, *, arrived: dict[str, Any]) -> frozenset[str]:
        if arrived == refused_doc:
            raise PermissionDeniedError("can_write_data required")
        return frozenset()

    return gate


@pytest.mark.parametrize("kind", ["create", "run"])
def test_a_refusal_whose_record_fails_stays_staged_and_the_tick_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, catalog: Any) -> None:
    """[[LH-297]] The record and the drop run inside the refusal handler, which the per-event
    `except Exception` does not cover, so a raise there would leave the loop and strand every later
    event. The verdict stands and is counted; the object stays staged until its record lands."""
    settings = _settings(tmp_path, fga_enabled=False)
    key, run_id = _stage_a_refusable_first(settings, kind, catalog)
    monkeypatch.setattr(relay, "enforce_bus_authz", _refusing(key, settings))
    repo = _RefusalLedgerDown()

    outcome = _drain(settings, repo, BusDouble())

    assert (outcome.drained, outcome.refused, outcome.recorded, outcome.stranded) == (1, 1, 0, 0), (
        f"the tick misreported a refusal it could not record: {outcome}"
    )
    assert [event.run.run_id for event in _runs(repo)] == [run_id], "a refusal whose record failed aborted the tick"
    assert outbox.resolve_event(settings.outbox_uri, StorageOptions(), key) is not None, "a refusal was retired before its record landed"


def test_a_refusal_whose_retirement_fails_is_recorded_and_the_tick_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any) -> None:
    """The other half of the same try: the record lands, the delete raises, and the next event still drains."""
    settings = _settings(tmp_path, fga_enabled=False)
    key, run_id = _stage_a_refusable_first(settings, "create", catalog)
    monkeypatch.setattr(relay, "enforce_bus_authz", _refusing(key, settings))
    real_drop = outbox.drop_event

    def drop(uri: str, opts: StorageOptions, dropped: str) -> None:
        if dropped == key:
            raise TimeoutError("store timed out")
        real_drop(uri, opts, dropped)

    monkeypatch.setattr(outbox, "drop_event", drop)
    repo = ProvenanceStoreDouble()

    outcome = _drain(settings, repo, BusDouble())

    assert (outcome.drained, outcome.refused, outcome.recorded) == (1, 1, 1), f"a failed retirement misreported the tick: {outcome}"
    assert [event.run.run_id for event in _runs(repo)] == [run_id], "a refusal whose delete failed aborted the tick"
    assert outbox.resolve_event(settings.outbox_uri, StorageOptions(), key) is not None, "the undeleted object should wait for the next tick"


def test_a_poison_object_whose_delete_fails_does_not_stop_the_create_behind_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any) -> None:
    """The poison branch's delete is outside the per-event `except Exception` too; one that raised aborted the tick."""
    settings = _settings(tmp_path, fga_enabled=False)
    _stage_a_create(settings, catalog)
    hostile = _stage_hostile_first(settings, _HOSTILE["bigint"])
    monkeypatch.setattr(relay, "enforce_bus_authz", _admit)
    real_drop = outbox.drop_event

    def drop(uri: str, opts: StorageOptions, key: str) -> None:
        if key == hostile:
            raise TimeoutError("store timed out")
        real_drop(uri, opts, key)

    monkeypatch.setattr(outbox, "drop_event", drop)
    repo = ProvenanceStoreDouble()

    outcome = _drain(settings, repo, BusDouble())

    assert outcome.drained == 1 and [e.dataset.name for e in _datasets(repo)] == [_TABLE], f"a failed poison delete aborted the tick: {outcome}"
    assert outbox.resolve_event(settings.outbox_uri, StorageOptions(), hostile) is not None, "the undeletable poison should wait for the next tick"


def test_a_create_the_feed_already_holds_is_a_replay_and_not_a_loss(
    tmp_path: Path, openfga_store: Any, catalog: Any, event_signer: Any, secret_store: SecretStoreDouble
) -> None:
    """A staged copy of a create the feed already holds byte-for-byte (published, then the stager's own
    delete failed) is a redelivery, even when its author has since lost the grant. It is signed by a listed signer that
    is no delegator, which the gate checks in full, so the refusal it meets is what the replay answers. The feed keys a
    static change by its derived id, never by a run id it does not have."""
    other = event_signer("service-pipeline-a")
    secret_store.hold(f"signing-public-{other.identity}", {"keys": other.public})
    settings = _settings(tmp_path, fga_enabled=True, signers=frozenset({_CATALOG, other.identity}))
    _, staged_json = _stage_a_create(settings, other, author=other.identity)
    _arm_gate(openfga_store, writer="dex~somebody-else")
    held = DatasetEvent.model_validate(json.loads(staged_json))

    class _FeedHoldsIt(ProvenanceStoreDouble):
        async def recorded_event(self, key: str) -> dict[str, Any] | None:
            return held.model_dump(by_alias=True) if key == feed_key(held) else None

    repo = _FeedHoldsIt()

    outcome = _drain(settings, repo, BusDouble(), openfga_store)

    assert (outcome.drained, outcome.refused) == (1, 0), f"a redelivered create was filed as lost provenance: {outcome}"
    assert repo.refusals == []


# --------------------------------------------------------------------------- #
# The DLQ view and replay
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("unsigned", [False, True], ids=["an-operator-who-may-not-write", "an-event-no-listed-signer-signed"])
def test_the_dlq_replay_refuses_a_create_it_may_not_record_and_leaves_it_staged(tmp_path: Path, openfga_store: Any, catalog: Any, unsigned: bool) -> None:  # noqa: ANN401
    """An operator who sees the table and may not write it is refused by the write rung; one who may write it is still
    refused an event no listed signer signed."""
    settings = _settings(tmp_path, fga_enabled=True)
    key, staged_json = _stage_a_create(settings, catalog)
    if unsigned:
        _rewrite_staged(settings, key, _without_signature(staged_json))
    _arm_gate(openfga_store, writer="dex~operator" if unsigned else None, readers=("dex~operator",))
    operator = person("operator")
    repo, publisher = ProvenanceStoreDouble(), BusDouble()

    with pytest.raises(PermissionDeniedError) as refused:
        _decided(
            openfga_store,
            lambda request: dlq.replay_dlq(
                run_id=key,
                request=request,
                provenance=cast(ProvenanceStore, repo),
                settings=settings,
                token=operator,
                datasets=fga_deps.DatasetFilter(request, settings, operator),
                publisher=publisher,
            ),
        )

    assert isinstance(refused.value, UnverifiedEventError) is unsigned, f"the refusal came from the wrong gate: {refused.value!r}"
    assert repo.recorded == [] and publisher.events == [], "a refused replay reached the feed or the bus"
    assert outbox.resolve_event(settings.outbox_uri, StorageOptions(), key) is not None, "a refused replay must leave the object staged"


def test_the_dlq_replay_re_publishes_the_staged_event_to_the_lineage_topic(tmp_path: Path, catalog: Any) -> None:
    """The relay's manual twin tells the SUBSCRIBERS too: a replayed create reaches the bus's subscribers only this way."""
    settings = _settings(tmp_path, fga_enabled=False)
    key, staged_json = _stage_a_create(settings, catalog)
    publisher = BusDouble()

    out = asyncio.run(
        dlq.replay_dlq(
            run_id=key,
            request=_request(),
            provenance=cast(ProvenanceStore, ProvenanceStoreDouble()),
            settings=settings,
            token=None,
            datasets=fga_deps.DatasetFilter(_request(), settings, None),
            publisher=publisher,
        )
    )

    assert out.status == "replayed"
    assert publisher.events == [json.loads(staged_json)], "subscribers never hear of a replayed create; the re-publish must carry the staged event"
    assert [sent.topic for sent in publisher.sent] == [settings.lineage_topic], "the replay announced the create somewhere other than the lineage topic"
    assert outbox.resolve_event(settings.outbox_uri, StorageOptions(), key) is None, "a replayed event must be dropped"


@pytest.mark.parametrize(
    ("failing", "answer", "saying"),
    [
        pytest.param(BusUnavailableError("this process is not connected to the bus"), ServiceUnavailableError, "re-announcing it failed", id="the-bus-is-away"),
        pytest.param(
            ValueError("event payload is 1048577 bytes (> 1048576)"), ValueError, "event payload is 1048577 bytes", id="an-envelope-past-the-payload-cap"
        ),
        pytest.param(KeyError("a fault of the door's own"), KeyError, "a fault of the door's own", id="a-fault-the-publish-does-not-document"),
    ],
)
def test_a_replay_whose_publish_fails_leaves_the_object_for_the_relay_and_is_audited_as_failed(
    tmp_path: Path, catalog: Any, caplog: pytest.LogCaptureFixture, failing: Exception, answer: type[Exception], saying: str
) -> None:
    """A bus that did not take the copy answers the retryable 503. An envelope past the payload cap is
    refused the same on every retry, and a failure the publish does not document is a fault: each answers as itself,
    never as an outage to retry. Whichever it is, the trail says the replay failed."""
    settings = _settings(tmp_path, fga_enabled=False)
    key, _ = _stage_a_create(settings, catalog)

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER), pytest.raises(answer, match=saying):
        asyncio.run(
            dlq.replay_dlq(
                run_id=key,
                request=_request(),
                provenance=cast(ProvenanceStore, ProvenanceStoreDouble()),
                settings=settings,
                token=None,
                datasets=fga_deps.DatasetFilter(_request(), settings, None),
                publisher=BusDouble(failing=failing),
            )
        )

    outcomes = [
        getattr(record, "audit.outcome") for record in caplog.records if record.name == AUDIT_LOGGER and getattr(record, "audit.action") == "dlq_replay"
    ]
    assert outcomes == ["failure"]
    assert outbox.resolve_event(settings.outbox_uri, StorageOptions(), key) is not None, "the drop destroyed the copy the relay would still have re-published"


@pytest.mark.parametrize("body", [_HOSTILE["bigint"]], ids=["bigint"])
def test_the_dlq_view_lists_bytes_no_parser_accepts_as_poison(tmp_path: Path, body: str, catalog: Any) -> None:
    settings = _settings(tmp_path, fga_enabled=False)
    key, _ = _stage_a_create(settings, catalog)
    hostile = _stage_hostile_first(settings, body)

    backlog = asyncio.run(dlq.list_dlq(settings=settings, datasets=fga_deps.DatasetFilter(_request(), settings, None), limit=100))

    assert [(e.run_id, e.parseable) for e in backlog.events] == [(hostile, False), (key, True)], f"the view does not show the poison: {backlog.events}"


@pytest.mark.parametrize(
    ("fga_enabled", "answer"), [(False, UnsupportedOperationError), (True, TransactionNotFoundError)], ids=["auth-off-422", "governed-404"]
)
@pytest.mark.parametrize("body", [_HOSTILE["bigint"]], ids=["bigint"])
def test_the_dlq_replay_answers_bytes_no_parser_accepts_as_poison(tmp_path: Path, body: str, fga_enabled: bool, answer: type[Exception]) -> None:
    settings = _settings(tmp_path, fga_enabled=fga_enabled)
    hostile = _stage_hostile_first(settings, body)
    flt = fga_deps.DatasetFilter(_request(), settings, None)

    with pytest.raises(answer):
        asyncio.run(
            dlq.replay_dlq(
                run_id=hostile,
                request=_request(),
                provenance=cast(ProvenanceStore, ProvenanceStoreDouble()),
                settings=settings,
                token=None,
                datasets=flt,
                publisher=BusDouble(),
            )
        )

    assert outbox.resolve_event(settings.outbox_uri, StorageOptions(), hostile) is not None, "a poison replay must leave the object for the relay"


def test_a_create_for_a_table_the_operator_cannot_see_is_neither_listed_nor_replayable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openfga_store: Any, catalog: Any
) -> None:
    """The view hides it, and the replay answers exactly as for an absent key: no 403 naming the table."""
    settings = _settings(tmp_path, fga_enabled=True)
    key, _ = _stage_a_create(settings, catalog)
    _arm_gate(openfga_store, writer=_AUTHOR)
    stranger = person("stranger")
    audited: list[tuple[str, str]] = []
    monkeypatch.setattr(dlq.audit, "audit", lambda action, outcome, **_kw: audited.append((action, outcome)))
    repo, publisher = ProvenanceStoreDouble(), BusDouble()

    backlog = _decided(openfga_store, lambda request: dlq.list_dlq(settings=settings, datasets=fga_deps.DatasetFilter(request, settings, stranger), limit=100))
    with pytest.raises(TransactionNotFoundError, match=f"no staged lineage event for run {key}"):
        _decided(
            openfga_store,
            lambda request: dlq.replay_dlq(
                run_id=key,
                request=request,
                provenance=cast(ProvenanceStore, repo),
                settings=settings,
                token=stranger,
                datasets=fga_deps.DatasetFilter(request, settings, stranger),
                publisher=publisher,
            ),
        )

    assert backlog.events == [], f"the view lists a create for a table the caller cannot see: {backlog.events}"
    assert _datasets(repo) == [] and publisher.events == [] and audited == []


def test_the_dlq_replay_re_publishes_a_run_event(tmp_path: Path, catalog: Any) -> None:
    """A replayed write RunEvent reaches the lineage topic as it was staged, so a subscriber on the bus hears it again."""
    settings = _settings(tmp_path, fga_enabled=False)
    event = build_write_event(
        table_id="raw$events",
        uuids=_uuids("raw$events"),
        namespace="raw",
        author=_AUTHOR,
        version=2,
        operation=INSERT,
        run_id=run_id_for("replayed-head"),
        event_time="2026-10-12T00:00:00+00:00",
        job_namespace="lance-catalog",
    )
    event = catalog.sign(event, on_behalf_of=_AUTHOR)
    outbox.stage_envelope(settings.outbox_uri, StorageOptions(), lineage_envelope(event))
    [(key, _)] = list(outbox.list_events(settings.outbox_uri, StorageOptions()))
    publisher = BusDouble()

    out = asyncio.run(
        dlq.replay_dlq(
            run_id=key,
            request=_request(),
            provenance=cast(ProvenanceStore, ProvenanceStoreDouble()),
            settings=settings,
            token=None,
            datasets=fga_deps.DatasetFilter(_request(), settings, None),
            publisher=publisher,
        )
    )

    assert out.status == "replayed"
    assert publisher.events == [event], "a replayed run never reaches the lineage topic"
