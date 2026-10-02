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
respx at the sidecar's address), so every test below reaches its assertion through a verified event; the signature is made by the
root conftest's `EventSigner`, which writes the wire format from the contract alone.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override

import httpx
import pytest
import respx
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, TransactionNotFoundError, UnsupportedOperationError

from catalog.core.lineage_emit import build_write_event
from lineage.api import fga_deps, reconcile_cron
from lineage.api.v1.endpoints import dlq
from lineage.core.config import LineageSettings, Signing
from lineage.models import DatasetEvent, UnverifiedEventError
from lineage_kit.signing import signature_of
from medallion.schemas.events import build_run_event
from service_kit.governed import fga
from service_kit.governed.oidc import IDToken
from service_kit.lakehouse import outbox, outbox_metrics
from service_kit.openlineage import event_identity


_CATALOG = "service-catalog"
_SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"
_AUTHOR = "CgVhbGljZRIFbG9jYWw"
_TABLE = "gov$bronze"
#: Bytes `json.loads` refuses with something other than `JSONDecodeError`: a plain `ValueError` past 4300
#: digits and a `RecursionError` past the nesting limit. Any stager holding the outbox credential can write them.
_HOSTILE = {"bigint": '{"eventType": ' + "1" * 5000 + "}", "deep": "[" * 100_000 + "]" * 100_000}


class _SidecarDown:
    async def publish_event(self, **_kwargs: object) -> None:
        raise ConnectionError("sidecar unreachable")


class _Publisher:
    """The Dapr client's `publish_event` signature, so a call the real client would refuse fails here too."""

    def __init__(self) -> None:
        self.published: list[str] = []
        self.topics: list[tuple[str, str]] = []

    async def publish_event(
        self,
        pubsub_name: str,
        topic_name: str,
        data: bytes | str,
        publish_metadata: dict[str, str] | None = None,
        metadata: tuple[tuple[str, str | bytes], ...] | None = None,
        data_content_type: str | None = None,
    ) -> None:
        self.topics.append((pubsub_name, topic_name))
        self.published.append(data if isinstance(data, str) else data.decode())


class _Repo:
    """Both ingest doors, so the assertion is WHICH one the relay took."""

    def __init__(self) -> None:
        self.runs: list[Any] = []
        self.datasets: list[DatasetEvent] = []
        self.refusals: list[dict[str, str | None]] = []

    async def ingest_event(self, event: Any) -> None:  # noqa: ANN401 — the doors' own shape
        self.runs.append(event)

    async def ingest_dataset_event(self, event: DatasetEvent) -> None:
        self.datasets.append(event)

    async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
        self.refusals.append({"outbox_key": outbox_key, "run_id": run_id, "author": author, "reason": reason, "event_json": event_json})


@pytest.fixture
def catalog(event_signer: Any, respx_allows_unused_routes: None) -> Iterator[Any]:
    """The catalog's signing identity, its public key served where the Dapr sidecar serves a secret."""
    signer = event_signer(_CATALOG)
    with respx.mock:
        respx.get(f"{_SECRETS}/signing-public-{_CATALOG}").mock(return_value=httpx.Response(200, json={"keys": signer.public}))
        yield signer


def _settings(tmp_path: Path, *, fga_enabled: bool, signers: frozenset[str] = frozenset({_CATALOG})) -> LineageSettings:
    """Signing enforced, the catalog the one delegator and `signers` the identities that may sign, as the chart renders it."""
    auth = {"oidc_enabled": True, "oidc_issuer": "https://dex.example", "oidc_audience": "lance", "fga_store_id": "s", "fga_model_id": "m"}
    return LineageSettings.model_validate(
        {
            "database_url": "postgresql://x/y",
            "outbox_uri": str(tmp_path / "outbox"),
            "fga_enabled": fga_enabled,
            "signing": Signing(signers=signers, delegators=frozenset({_CATALOG})),
            **(auth if fga_enabled else {}),
        }
    )


def _create_event(catalog: Any, author: str = _AUTHOR) -> dict[str, Any]:  # noqa: ANN401 - the conftest's EventSigner
    """The catalog's create for `_TABLE`, from its real builder, signed as the catalog on behalf of `author`."""
    event = build_write_event(
        table_id=_TABLE,
        namespace="lance",
        author=author,
        version=1,
        operation="create_table",
        run_id="0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a60",
        event_time="2026-10-02T12:00:00+00:00",
        job_namespace="lance",
    )
    return catalog.sign(event, on_behalf_of=author)


def _stage_a_create(settings: LineageSettings, catalog: Any) -> tuple[str, str]:  # noqa: ANN401 - the conftest's EventSigner
    """What the catalog leaves in the outbox when its publish fails: its create event, signed."""
    doc = _create_event(catalog)
    outbox.stage_event(settings.outbox_uri, {}, event_identity(doc), json.dumps(doc))
    staged = list(outbox.list_events(settings.outbox_uri, {}))
    assert len(staged) == 1, f"staging left {len(staged)} objects, not one"
    key, event_json = staged[0]
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


def _arm_gate(monkeypatch: pytest.MonkeyPatch, *, writer: str | None) -> None:
    """The real `enforce_bus_authz`: the catalog's real key, and an FGA that lets `writer` write the table."""

    async def batch_check(_client: object, *, user: str, relation: str, objects: list[str], **_kw: object) -> dict[str, bool]:
        return dict.fromkeys(objects, user == writer and relation in ("can_write_data", "can_get_metadata"))

    async def read_object_tuples(_client: object, _obj: str) -> list[object]:
        return [object()]  # governed, so a denial is repairable rather than unrepairable

    monkeypatch.setattr(fga, "batch_check", batch_check)
    monkeypatch.setattr(fga, "read_object_tuples", read_object_tuples)


def _request() -> Any:  # noqa: ANN401 — the drain and the routes read only app.state
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fga=object())))


def _rewrite_staged(settings: LineageSettings, key: str, doc: dict[str, Any]) -> None:
    """Overwrite a staged object in place, as a stager holding the outbox credential can."""
    (Path(settings.outbox_uri) / f"{key}.json").write_text(json.dumps(doc))


def _without_signature(staged_json: str) -> dict[str, Any]:
    doc = json.loads(staged_json)
    doc["dataset"]["facets"].pop("rask_signature")
    assert signature_of(doc) is None
    return doc


def _restamped(catalog: Any, author: str) -> dict[str, Any]:  # noqa: ANN401 - the conftest's EventSigner
    """The same create, stamped for another subject and signed again: a valid signature over an author who may not write."""
    doc = _create_event(catalog, author)
    assert signature_of(doc) is not None
    return doc


def _drain(settings: LineageSettings, repo: _Repo, publisher: _Publisher) -> reconcile_cron.DrainOutcome:
    return asyncio.run(reconcile_cron._drain_outbox(_request(), cast("Any", repo), settings, {}, publisher))


def _stage_a_run_behind(settings: LineageSettings, create_key: str) -> str:
    """A run event staged AFTER the create, so the drain meets the create first and must still reach this."""
    event = build_run_event(
        operation="ingest_events",
        author=_AUTHOR,
        job_namespace="medallion",
        inputs=[("bronze", "bronze$events")],
        output_namespace="bronze",
        output_name="bronze$events",
        version=2,
        token="behind-the-create",
    )
    run_id = event["run"]["runId"]
    outbox.stage_event(settings.outbox_uri, {}, run_id, json.dumps(event))
    os.utime(Path(settings.outbox_uri) / f"{create_key}.json", ns=(1, 1))
    return run_id


async def _admit(*_args: object) -> None:
    """The gate admits: what is under test is the drain's handling after it, not the gate's decision."""


class _CreateDoorDown(_Repo):
    @override
    async def ingest_dataset_event(self, event: DatasetEvent) -> None:
        raise ConnectionError("graph unreachable")


class _RefusalLedgerDown(_Repo):
    @override
    async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
        raise ConnectionError("postgres unreachable")


# --------------------------------------------------------------------------- #
# The relay
# --------------------------------------------------------------------------- #


def test_the_drain_re_ingests_a_staged_create_through_the_dataset_door(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any) -> None:
    settings = _settings(tmp_path, fga_enabled=True)
    _, staged_json = _stage_a_create(settings, catalog)
    _arm_gate(monkeypatch, writer=_AUTHOR)
    repo, publisher = _Repo(), _Publisher()

    outcome = _drain(settings, repo, publisher)

    assert outcome.drained == 1, f"the staged create was not recovered: {outcome}"
    assert [e.dataset.name for e in repo.datasets] == [_TABLE] and repo.runs == [], "the create did not reach the dataset door"
    assert repo.datasets[0].operation == "create_table"
    assert publisher.published == [staged_json], "subscribers never hear of the create; the re-publish must carry the staged bytes"
    assert not list(outbox.list_events(settings.outbox_uri, {})), "a recovered event must not stay staged"


@pytest.mark.parametrize(
    ("unsigned", "writer", "reason_names"),
    [
        pytest.param(False, "somebody-else", "can_write_data", id="a-stamped-author-who-may-not-write"),
        pytest.param(True, _AUTHOR, "no signature", id="an-event-no-listed-signer-signed"),
    ],
)
def test_a_staged_create_the_gate_refuses_is_refused_and_never_ingested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any, unsigned: bool, writer: str, reason_names: str
) -> None:
    """The DatasetEvent path still runs `enforce_bus_authz`: a stamped author who may not write is refused, and so is an event no
    listed signer signed. Both are recorded and retired, never ingested and never announced: a retry cannot change either answer."""
    settings = _settings(tmp_path, fga_enabled=True)
    key, staged_json = _stage_a_create(settings, catalog)
    if unsigned:
        staged_json = json.dumps(_without_signature(staged_json))
        _rewrite_staged(settings, key, json.loads(staged_json))
    _arm_gate(monkeypatch, writer=writer)
    repo, publisher = _Repo(), _Publisher()

    outcome = _drain(settings, repo, publisher)

    assert outcome.refused == outcome.recorded == 1 and outcome.drained == 0, f"a refused create was not handled as a refusal: {outcome}"
    assert repo.datasets == [] and repo.runs == [] and publisher.published == [], "a refused create reached the graph or the bus"
    # No run to name: the key is what identifies a static change, and `run_id` stays empty rather than lying.
    [refusal] = repo.refusals
    assert reason_names in str(refusal["reason"]), f"the recorded reason does not say what was refused: {refusal['reason']}"
    assert refusal == {"outbox_key": key, "run_id": None, "author": _AUTHOR, "reason": refusal["reason"], "event_json": staged_json}
    assert not list(outbox.list_events(settings.outbox_uri, {})), "a recorded refusal must retire the staged object"


@pytest.mark.parametrize(
    ("operation", "by_the_catalog", "drained"),
    [
        pytest.param("drop_table", True, 1, id="a-delegators-drop-is-admitted-though-its-grants-are-gone"),
        pytest.param("create_table", True, 0, id="a-delegators-create-keeps-the-full-check"),
        pytest.param("drop_table", False, 0, id="another-listed-signers-drop-keeps-the-full-check"),
    ],
)
def test_the_relay_admits_a_delegators_drop_without_a_grant_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any, event_signer: Any, operation: str, by_the_catalog: bool, drained: int
) -> None:
    """The catalog revokes a table's grants in the request that drops it, so a drop delivered after the revoke asks FGA about a
    table nobody may write. A drop whose signature verifies as a delegator is the catalog's attestation that it enforced `can_drop`,
    and is admitted without the grant; everything else, a delegator's create or a drop signed by any other listed signer, still
    needs it."""
    other = event_signer("service-bronze-to-silver")
    respx.get(f"{_SECRETS}/signing-public-{other.identity}").mock(return_value=httpx.Response(200, json={"keys": other.public}))
    settings = _settings(tmp_path, fga_enabled=True, signers=frozenset({_CATALOG, other.identity}))
    author = _AUTHOR if by_the_catalog else other.identity
    event = build_write_event(
        table_id=_TABLE,
        namespace="lance",
        author=author,
        version=None,
        operation=operation,
        run_id="0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a61",
        event_time="2026-10-02T12:00:00+00:00",
        job_namespace="lance",
    )
    signed = catalog.sign(event, on_behalf_of=author) if by_the_catalog else other.sign(event)
    outbox.stage_event(settings.outbox_uri, {}, event_identity(signed), json.dumps(signed))
    _arm_gate(monkeypatch, writer=None)
    repo = _Repo()

    outcome = _drain(settings, repo, _Publisher())

    assert (outcome.drained, outcome.refused) == (drained, 1 - drained), f"the relay answered {outcome}"
    assert [(e.dataset.name, e.operation) for e in repo.datasets] == ([(_TABLE, operation)] if drained else [])


@pytest.mark.parametrize("body", _HOSTILE.values(), ids=_HOSTILE.keys())
def test_bytes_no_parser_accepts_are_poison_and_do_not_wedge_the_create_behind_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, catalog: Any
) -> None:
    """Read first, such an object is dropped as poison; one escaping the parse would abort every tick."""
    settings = _settings(tmp_path, fga_enabled=True)
    _stage_a_create(settings, catalog)
    hostile = _stage_hostile_first(settings, body)
    _arm_gate(monkeypatch, writer=_AUTHOR)
    poison: list[None] = []
    monkeypatch.setattr(outbox_metrics, "record_poison_dropped", lambda: poison.append(None))
    repo = _Repo()

    outcome = _drain(settings, repo, _Publisher())

    assert (outcome.drained, len(poison)) == (1, 1), f"the hostile object was not handled as poison: {outcome}, poison={len(poison)}"
    assert [e.dataset.name for e in repo.datasets] == [_TABLE], "the create staged behind the hostile object was not recovered"
    assert outbox.resolve_event(settings.outbox_uri, {}, hostile) is None, "the poison object stays staged to wedge the next tick"


def test_a_create_whose_ingest_fails_is_stranded_and_the_run_behind_it_still_drains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any) -> None:
    """PER-EVENT ISOLATION holds for the new event kind: a transient failure on a create strands that
    object and the tick goes on. The stranded log names `event.run_id`, which a `DatasetEvent` answers
    with `None`; `event.run.run_id` there would raise AttributeError out of the handler and abort the tick."""
    settings = _settings(tmp_path, fga_enabled=False)
    key, _ = _stage_a_create(settings, catalog)
    run_id = _stage_a_run_behind(settings, key)
    monkeypatch.setattr(reconcile_cron, "enforce_bus_authz", _admit)
    repo = _CreateDoorDown()

    outcome = _drain(settings, repo, _Publisher())

    assert (outcome.drained, outcome.stranded, outcome.refused) == (1, 1, 0), f"one failed create aborted or misreported the tick: {outcome}"
    assert [event.run.run_id for event in repo.runs] == [run_id], "the run staged behind the failed create was not recovered"
    assert outbox.resolve_event(settings.outbox_uri, {}, key) is not None, "a transient failure destroyed the create's only durable copy"


def _stage_a_refusable_first(settings: LineageSettings, kind: str, catalog: Any) -> tuple[str, str]:  # noqa: ANN401 - the conftest's EventSigner
    """The event the gate will refuse, staged oldest, and a run staged behind it. Returns (refused key, run id behind)."""
    if kind == "create":
        key, _ = _stage_a_create(settings, catalog)
        return key, _stage_a_run_behind(settings, key)
    event = build_run_event(
        operation="ingest_events",
        author=_AUTHOR,
        job_namespace="medallion",
        inputs=[("bronze", "bronze$events")],
        output_namespace="bronze",
        output_name="bronze$events",
        version=1,
        token="refused-run",
    )
    outbox.stage_event(settings.outbox_uri, {}, event["run"]["runId"], json.dumps(event))
    [(key, _)] = list(outbox.list_events(settings.outbox_uri, {}))
    return key, _stage_a_run_behind(settings, key)


def _refusing(key: str, settings: LineageSettings) -> Any:  # noqa: ANN401 — the gate's own signature
    """A gate that refuses exactly the event staged under `key` and admits the rest."""
    refused_json = outbox.resolve_event(settings.outbox_uri, {}, key)
    assert refused_json is not None
    refused_doc = json.loads(refused_json[1])

    async def gate(_event: object, _request: object, _settings: object, arrived: dict[str, Any]) -> None:
        if arrived == refused_doc:
            raise PermissionDeniedError("can_write_data required")

    return gate


@pytest.mark.parametrize("kind", ["create", "run"])
def test_a_refusal_whose_record_fails_stays_staged_and_the_tick_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, catalog: Any) -> None:
    """[[LH-297]] The record and the drop run inside the refusal handler, which the per-event
    `except Exception` does not cover, so a raise there would leave the loop and strand every later
    event. The verdict stands and is counted; the object stays staged until its record lands."""
    settings = _settings(tmp_path, fga_enabled=False)
    key, run_id = _stage_a_refusable_first(settings, kind, catalog)
    monkeypatch.setattr(reconcile_cron, "enforce_bus_authz", _refusing(key, settings))
    repo = _RefusalLedgerDown()

    outcome = _drain(settings, repo, _Publisher())

    assert (outcome.drained, outcome.refused, outcome.recorded, outcome.stranded) == (1, 1, 0, 0), (
        f"the tick misreported a refusal it could not record: {outcome}"
    )
    assert [event.run.run_id for event in repo.runs] == [run_id], "a refusal whose record failed aborted the tick"
    assert outbox.resolve_event(settings.outbox_uri, {}, key) is not None, "a refusal was retired before its record landed"


def test_a_refusal_whose_retirement_fails_is_recorded_and_the_tick_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any) -> None:
    """The other half of the same try: the record lands, the delete raises, and the next event still drains."""
    settings = _settings(tmp_path, fga_enabled=False)
    key, run_id = _stage_a_refusable_first(settings, "create", catalog)
    monkeypatch.setattr(reconcile_cron, "enforce_bus_authz", _refusing(key, settings))
    real_drop = outbox.drop_event

    def drop(uri: str, opts: dict[str, str], dropped: str) -> None:
        if dropped == key:
            raise TimeoutError("store timed out")
        real_drop(uri, opts, dropped)

    monkeypatch.setattr(outbox, "drop_event", drop)
    repo = _Repo()

    outcome = _drain(settings, repo, _Publisher())

    assert (outcome.drained, outcome.refused, outcome.recorded) == (1, 1, 1), f"a failed retirement misreported the tick: {outcome}"
    assert [event.run.run_id for event in repo.runs] == [run_id], "a refusal whose delete failed aborted the tick"
    assert outbox.resolve_event(settings.outbox_uri, {}, key) is not None, "the undeleted object should wait for the next tick"


def test_a_poison_object_whose_delete_fails_does_not_stop_the_create_behind_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any) -> None:
    """The poison branch's delete is outside the per-event `except Exception` too; one that raised aborted the tick."""
    settings = _settings(tmp_path, fga_enabled=False)
    _stage_a_create(settings, catalog)
    hostile = _stage_hostile_first(settings, _HOSTILE["bigint"])
    monkeypatch.setattr(reconcile_cron, "enforce_bus_authz", _admit)
    real_drop = outbox.drop_event

    def drop(uri: str, opts: dict[str, str], key: str) -> None:
        if key == hostile:
            raise TimeoutError("store timed out")
        real_drop(uri, opts, key)

    monkeypatch.setattr(outbox, "drop_event", drop)
    repo = _Repo()

    outcome = _drain(settings, repo, _Publisher())

    assert outcome.drained == 1 and [e.dataset.name for e in repo.datasets] == [_TABLE], f"a failed poison delete aborted the tick: {outcome}"
    assert outbox.resolve_event(settings.outbox_uri, {}, hostile) is not None, "the undeletable poison should wait for the next tick"


def test_a_create_the_feed_already_holds_is_a_replay_and_not_a_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any) -> None:
    """A staged copy of a create the feed already holds byte-for-byte (published, then the stager's own
    delete failed) is a redelivery, even when its author has since lost the grant. The feed keys a
    static change by its derived id, never by a run id it does not have."""
    settings = _settings(tmp_path, fga_enabled=True)
    _, staged_json = _stage_a_create(settings, catalog)
    _arm_gate(monkeypatch, writer="somebody-else")
    held = DatasetEvent.model_validate(json.loads(staged_json))

    class _FeedHoldsIt(_Repo):
        async def recorded_event(self, run_id: str, event_type: str | None) -> dict[str, Any] | None:
            return held.model_dump(by_alias=True) if (run_id, event_type) == (held.feed_id, held.feed_event_type) else None

    repo = _FeedHoldsIt()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fga=object(), repository=repo)))

    outcome = asyncio.run(reconcile_cron._drain_outbox(cast("Any", request), cast("Any", repo), settings, {}, _Publisher()))

    assert (outcome.drained, outcome.refused) == (1, 0), f"a redelivered create was filed as lost provenance: {outcome}"
    assert repo.refusals == []


# --------------------------------------------------------------------------- #
# The DLQ view and replay
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("unsigned", [False, True], ids=["an-operator-who-may-not-write", "an-event-no-listed-signer-signed"])
def test_the_dlq_replay_refuses_a_create_it_may_not_record_and_leaves_it_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any, unsigned: bool
) -> None:
    settings = _settings(tmp_path, fga_enabled=True)
    key, staged_json = _stage_a_create(settings, catalog)
    if unsigned:
        _rewrite_staged(settings, key, _without_signature(staged_json))

    async def see_not_write(_client: object, *, relation: str, objects: list[str], **_kw: object) -> dict[str, bool]:
        return dict.fromkeys(objects, relation == "can_get_metadata" or unsigned)

    monkeypatch.setattr(fga, "batch_check", see_not_write)
    operator = IDToken(iss="i", sub="operator", aud="lance", exp=0, iat=0)
    repo, publisher = _Repo(), _Publisher()

    with pytest.raises(PermissionDeniedError) as refused:
        asyncio.run(dlq.replay_dlq(key, _request(), cast("Any", repo), settings, operator, fga_deps.DatasetFilter(_request(), settings, operator), publisher))

    assert isinstance(refused.value, UnverifiedEventError) is unsigned, f"the refusal came from the wrong gate: {refused.value!r}"
    assert repo.datasets == [] and repo.runs == [] and publisher.published == [], "a refused replay reached the graph or the bus"
    assert outbox.resolve_event(settings.outbox_uri, {}, key) is not None, "a refused replay must leave the object staged"


def test_the_dlq_replay_re_publishes_the_staged_bytes_to_the_lineage_topic(tmp_path: Path, catalog: Any) -> None:
    """The relay's manual twin tells the SUBSCRIBERS too: a replayed create reaches the notifications bus lane only this way."""
    settings = _settings(tmp_path, fga_enabled=False)
    key, staged_json = _stage_a_create(settings, catalog)
    publisher = _Publisher()

    out = asyncio.run(dlq.replay_dlq(key, _request(), cast("Any", _Repo()), settings, None, fga_deps.DatasetFilter(_request(), settings, None), publisher))

    assert out.status == "replayed"
    assert publisher.published == [staged_json], "subscribers never hear of a replayed create; the re-publish must carry the staged bytes"
    assert publisher.topics == [(settings.dapr_pubsub, settings.dapr_topic)], "the replay announced the create somewhere other than the lineage topic"
    assert outbox.resolve_event(settings.outbox_uri, {}, key) is None, "a replayed event must be dropped"


def test_a_replay_whose_publish_fails_leaves_the_object_for_the_relay(tmp_path: Path, catalog: Any) -> None:
    settings = _settings(tmp_path, fga_enabled=False)
    key, _ = _stage_a_create(settings, catalog)

    with pytest.raises(ServiceUnavailableError, match="re-announcing it failed"):
        asyncio.run(dlq.replay_dlq(key, _request(), cast("Any", _Repo()), settings, None, fga_deps.DatasetFilter(_request(), settings, None), _SidecarDown()))

    assert outbox.resolve_event(settings.outbox_uri, {}, key) is not None, "the drop destroyed the copy the relay would still have re-published"


@pytest.mark.parametrize("body", [_HOSTILE["bigint"]], ids=["bigint"])
def test_the_dlq_view_lists_bytes_no_parser_accepts_as_poison(tmp_path: Path, body: str, catalog: Any) -> None:
    settings = _settings(tmp_path, fga_enabled=False)
    key, _ = _stage_a_create(settings, catalog)
    hostile = _stage_hostile_first(settings, body)

    backlog = asyncio.run(dlq.list_dlq(settings, None, fga_deps.DatasetFilter(_request(), settings, None), limit=100))

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
        asyncio.run(dlq.replay_dlq(hostile, _request(), cast("Any", _Repo()), settings, None, flt, _Publisher()))

    assert outbox.resolve_event(settings.outbox_uri, {}, hostile) is not None, "a poison replay must leave the object for the relay"


def test_the_dlq_replay_refuses_a_signed_create_its_stamped_author_may_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any) -> None:
    """The stamped author is authorized as well as the operator, and a valid signature does not change that: an operator who may
    write the table still cannot replay provenance the catalog attests for a subject who may not."""
    settings = _settings(tmp_path, fga_enabled=True)
    key, _ = _stage_a_create(settings, catalog)
    _rewrite_staged(settings, key, _restamped(catalog, "somebody-else"))
    _arm_gate(monkeypatch, writer="operator")
    operator = IDToken(iss="i", sub="operator", aud="lance", exp=0, iat=0)
    repo, publisher = _Repo(), _Publisher()

    with pytest.raises(PermissionDeniedError) as refused:
        asyncio.run(dlq.replay_dlq(key, _request(), cast("Any", repo), settings, operator, fga_deps.DatasetFilter(_request(), settings, operator), publisher))

    assert not isinstance(refused.value, UnverifiedEventError), "the signature was valid, so the refusal has to be the stamped author's missing grant"
    assert repo.datasets == [] and publisher.published == []


def test_a_create_for_a_table_the_operator_cannot_see_is_neither_listed_nor_replayable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any) -> None:
    """The view hides it, and the replay answers exactly as for an absent key: no 403 naming the table."""
    settings = _settings(tmp_path, fga_enabled=True)
    key, _ = _stage_a_create(settings, catalog)
    _arm_gate(monkeypatch, writer=_AUTHOR)
    stranger = IDToken(iss="i", sub="stranger", aud="lance", exp=0, iat=0)
    audited: list[tuple[str, str]] = []
    monkeypatch.setattr(dlq.audit, "audit", lambda action, outcome, **_kw: audited.append((action, outcome)))
    repo, publisher = _Repo(), _Publisher()

    backlog = asyncio.run(dlq.list_dlq(settings, stranger, fga_deps.DatasetFilter(_request(), settings, stranger), limit=100))
    with pytest.raises(TransactionNotFoundError, match=f"no staged lineage event for run {key}"):
        asyncio.run(dlq.replay_dlq(key, _request(), cast("Any", repo), settings, stranger, fga_deps.DatasetFilter(_request(), settings, stranger), publisher))

    assert backlog.events == [], f"the view lists a create for a table the caller cannot see: {backlog.events}"
    assert repo.datasets == [] and publisher.published == [] and audited == []


def test_the_dlq_replay_re_publishes_a_run_event(tmp_path: Path, catalog: Any) -> None:
    """The re-announce that restarts a halted cascade: a replayed bronze-write RunEvent reaches the lineage topic byte-for-byte."""
    settings = _settings(tmp_path, fga_enabled=False)
    event = build_run_event(
        operation="ingest_events",
        author=_AUTHOR,
        job_namespace="medallion",
        inputs=[("bronze", "bronze$events")],
        output_namespace="bronze",
        output_name="bronze$events",
        version=2,
        token="replayed-head",
    )
    event = catalog.sign(event, on_behalf_of=_AUTHOR)
    staged_json = json.dumps(event)
    outbox.stage_event(settings.outbox_uri, {}, event["run"]["runId"], staged_json)
    [(key, _)] = list(outbox.list_events(settings.outbox_uri, {}))
    publisher = _Publisher()

    out = asyncio.run(dlq.replay_dlq(key, _request(), cast("Any", _Repo()), settings, None, fga_deps.DatasetFilter(_request(), settings, None), publisher))

    assert out.status == "replayed"
    assert publisher.published == [staged_json], "a replayed head run never reaches /bronze-arrival"
