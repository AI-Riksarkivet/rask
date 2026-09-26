"""A catalog DDL event staged on a failed publish must be recovered by the relay, not deleted as poison.

[[LH-199]] A create, drop, deregister, alter, declare or register reaches the bus as an OpenLineage
`DatasetEvent` (`build_write_event` -> `_as_dataset_event`). The drain and both DLQ routes parsed staged
bytes as `RunEvent` only, so the staged copy of every catalog DDL announcement failed on
`eventType`/`run`/`job`, was logged `lineage_outbox_poison_dropped` and deleted — on exactly the bus
outage the outbox exists for. Measured on ef9d1e8e with the real emitter and the real drain:
`drained=0`, zero ingests, the object gone.

Staged the way the catalog stages it — `DaprEmitter` over a sidecar that refuses the publish — and read
back through the doors' own discriminator, so the producer and the relay cannot disagree unseen.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override

import pytest
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, TransactionNotFoundError, UnsupportedOperationError

from catalog.core.lineage_emit import DaprEmitter
from lineage.api import fga_deps, reconcile_cron
from lineage.api.v1.endpoints import dlq
from lineage.core.config import LineageSettings
from lineage.models import DatasetEvent
from lineage_kit.signing import SIGNATURE_FACET, signature_of
from medallion.schemas.events import build_run_event
from service_kit.governed import fga
from service_kit.governed.oidc import IDToken
from service_kit.lakehouse import outbox, outbox_metrics


_KEY = "FGjbWnUx1oRd7TqYpE4sLvZc0MhA6iK2eB9wQn3t"
_CATALOG = "service-catalog"
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


def _settings(tmp_path: Path, *, fga_enabled: bool) -> LineageSettings:
    auth = {"oidc_enabled": True, "oidc_issuer": "https://dex.example", "oidc_audience": "lance", "fga_store_id": "s", "fga_model_id": "m"}
    return LineageSettings.model_validate(
        {"database_url": "postgresql://x/y", "outbox_uri": str(tmp_path / "outbox"), "fga_enabled": fga_enabled, **(auth if fga_enabled else {})}
    )


def _stage_a_create(settings: LineageSettings) -> tuple[str, str]:
    """What the catalog leaves in the outbox when its publish fails: the real emitter, signed."""
    emitter = DaprEmitter(
        cast("Any", _SidecarDown()),
        "pubsub",
        "lineage.events.v1",
        job_namespace="lance",
        timeout_seconds=5.0,
        outbox_uri=settings.outbox_uri,
        service_identity=_CATALOG,
        token_resolver=lambda identity: _KEY if identity == _CATALOG else None,
    )
    asyncio.run(emitter.emit_create(table_id=_TABLE, namespace="lance", author=_AUTHOR, version=1))
    staged = list(outbox.list_events(settings.outbox_uri, {}))
    assert len(staged) == 1, f"the failed publish left {len(staged)} staged objects, not one"
    key, event_json = staged[0]
    doc = json.loads(event_json)
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

    monkeypatch.setattr(fga_deps, "dedicated_token_from_store", lambda _store: lambda identity: _KEY if identity == _CATALOG else None)
    monkeypatch.setattr(fga, "batch_check", batch_check)
    monkeypatch.setattr(fga, "read_object_tuples", read_object_tuples)


def _arm_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog's real key where the gate reads it: a signature is checked whether or not FGA is on."""
    monkeypatch.setattr(fga_deps, "dedicated_token_from_store", lambda _store: lambda identity: _KEY if identity == _CATALOG else None)


def _request() -> Any:  # noqa: ANN401 — the drain and the routes read only app.state
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fga=object())))


def _rewrite_staged(settings: LineageSettings, key: str, doc: dict[str, Any]) -> None:
    """Overwrite a staged object in place, as a stager holding the outbox credential can."""
    (Path(settings.outbox_uri) / f"{key}.json").write_text(json.dumps(doc))


def _retargeted(staged_json: str) -> dict[str, Any]:
    """The catalog's signed create, pointed at another table after it was signed."""
    doc = json.loads(staged_json)
    doc["dataset"]["name"] = "gov$someone-elses"
    return doc


def _unsigned(staged_json: str) -> dict[str, Any]:
    doc = json.loads(staged_json)
    doc["dataset"]["facets"].pop(SIGNATURE_FACET)
    assert signature_of(doc) is None
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


def test_the_drain_re_ingests_a_staged_create_through_the_dataset_door(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, fga_enabled=True)
    _, staged_json = _stage_a_create(settings)
    _arm_gate(monkeypatch, writer=_AUTHOR)
    repo, publisher = _Repo(), _Publisher()

    outcome = _drain(settings, repo, publisher)

    assert outcome.drained == 1, f"the staged create was not recovered: {outcome}"
    assert [e.dataset.name for e in repo.datasets] == [_TABLE] and repo.runs == [], "the create did not reach the dataset door"
    assert repo.datasets[0].operation == "create_table"
    assert publisher.published == [staged_json], "subscribers never hear of the create; the re-publish must carry the staged bytes"
    assert not list(outbox.list_events(settings.outbox_uri, {})), "a recovered event must not stay staged"


def test_a_staged_create_the_gate_refuses_is_refused_and_never_ingested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The DatasetEvent path still runs `enforce_bus_authz`: a stamped author who may not write is refused."""
    settings = _settings(tmp_path, fga_enabled=True)
    key, staged_json = _stage_a_create(settings)
    _arm_gate(monkeypatch, writer="somebody-else")
    repo, publisher = _Repo(), _Publisher()

    outcome = _drain(settings, repo, publisher)

    assert outcome.refused == outcome.recorded == 1 and outcome.drained == 0, f"a refused create was not handled as a refusal: {outcome}"
    assert repo.datasets == [] and repo.runs == [] and publisher.published == [], "a refused create reached the graph or the bus"
    # No run to name: the key is what identifies a static change, and `run_id` stays empty rather than lying.
    [refusal] = repo.refusals
    assert "can_write_data" in str(refusal["reason"]), f"the recorded reason does not say what was refused: {refusal['reason']}"
    assert refusal == {"outbox_key": key, "run_id": None, "author": _AUTHOR, "reason": refusal["reason"], "event_json": staged_json}


@pytest.mark.parametrize("body", _HOSTILE.values(), ids=_HOSTILE.keys())
def test_bytes_no_parser_accepts_are_poison_and_do_not_wedge_the_create_behind_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Read first, such an object is dropped as poison; one escaping the parse would abort every tick."""
    settings = _settings(tmp_path, fga_enabled=True)
    _stage_a_create(settings)
    hostile = _stage_hostile_first(settings, body)
    _arm_gate(monkeypatch, writer=_AUTHOR)
    poison: list[None] = []
    monkeypatch.setattr(outbox_metrics, "record_poison_dropped", lambda: poison.append(None))
    repo = _Repo()

    outcome = _drain(settings, repo, _Publisher())

    assert (outcome.drained, len(poison)) == (1, 1), f"the hostile object was not handled as poison: {outcome}, poison={len(poison)}"
    assert [e.dataset.name for e in repo.datasets] == [_TABLE], "the create staged behind the hostile object was not recovered"
    assert outbox.resolve_event(settings.outbox_uri, {}, hostile) is None, "the poison object stays staged to wedge the next tick"


def test_a_create_whose_ingest_fails_is_stranded_and_the_run_behind_it_still_drains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PER-EVENT ISOLATION holds for the new event kind: a transient failure on a create strands that
    object and the tick goes on. The stranded log names `event.run_id`, which a `DatasetEvent` answers
    with `None`; `event.run.run_id` there would raise AttributeError out of the handler and abort the tick."""
    settings = _settings(tmp_path, fga_enabled=False)
    key, _ = _stage_a_create(settings)
    run_id = _stage_a_run_behind(settings, key)
    monkeypatch.setattr(reconcile_cron, "enforce_bus_authz", _admit)
    repo = _CreateDoorDown()

    outcome = _drain(settings, repo, _Publisher())

    assert (outcome.drained, outcome.stranded, outcome.refused) == (1, 1, 0), f"one failed create aborted or misreported the tick: {outcome}"
    assert [event.run.run_id for event in repo.runs] == [run_id], "the run staged behind the failed create was not recovered"
    assert outbox.resolve_event(settings.outbox_uri, {}, key) is not None, "a transient failure destroyed the create's only durable copy"


def _stage_a_refusable_first(settings: LineageSettings, kind: str) -> tuple[str, str]:
    """The event the gate will refuse, staged oldest, and a run staged behind it. Returns (refused key, run id behind)."""
    if kind == "create":
        key, _ = _stage_a_create(settings)
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
def test_a_refusal_whose_record_fails_stays_staged_and_the_tick_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """[[LH-297]] The record and the drop run inside the refusal handler, which the per-event
    `except Exception` does not cover, so a raise there would leave the loop and strand every later
    event. The verdict stands and is counted; the object stays staged until its record lands."""
    settings = _settings(tmp_path, fga_enabled=False)
    key, run_id = _stage_a_refusable_first(settings, kind)
    monkeypatch.setattr(reconcile_cron, "enforce_bus_authz", _refusing(key, settings))
    repo = _RefusalLedgerDown()

    outcome = _drain(settings, repo, _Publisher())

    assert (outcome.drained, outcome.refused, outcome.recorded, outcome.stranded) == (1, 1, 0, 0), (
        f"the tick misreported a refusal it could not record: {outcome}"
    )
    assert [event.run.run_id for event in repo.runs] == [run_id], "a refusal whose record failed aborted the tick"
    assert outbox.resolve_event(settings.outbox_uri, {}, key) is not None, "a refusal was retired before its record landed"


def test_a_refusal_whose_retirement_fails_is_recorded_and_the_tick_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the same try: the record lands, the delete raises, and the next event still drains."""
    settings = _settings(tmp_path, fga_enabled=False)
    key, run_id = _stage_a_refusable_first(settings, "create")
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


def test_a_signed_create_retargeted_after_signing_is_refused_by_the_relay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The signature leg of the gate, for a DatasetEvent: bytes changed after signing never reach the graph."""
    settings = _settings(tmp_path, fga_enabled=True)
    key, staged_json = _stage_a_create(settings)
    _rewrite_staged(settings, key, _retargeted(staged_json))
    _arm_gate(monkeypatch, writer=_AUTHOR)
    repo, publisher = _Repo(), _Publisher()

    outcome = _drain(settings, repo, publisher)

    assert (outcome.refused, outcome.drained) == (1, 0), f"a tampered create was not refused: {outcome}"
    assert repo.datasets == [] and publisher.published == [], "a tampered create reached the graph or the bus"


def test_a_poison_object_whose_delete_fails_does_not_stop_the_create_behind_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The poison branch's delete is outside the per-event `except Exception` too; one that raised aborted the tick."""
    settings = _settings(tmp_path, fga_enabled=False)
    _stage_a_create(settings)
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


def test_a_create_the_feed_already_holds_is_a_replay_and_not_a_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A staged copy of a create the feed already holds byte-for-byte (published, then the stager's own
    delete failed) is a redelivery, even when its author has since lost the grant. The feed keys a
    static change by its derived id, never by a run id it does not have."""
    settings = _settings(tmp_path, fga_enabled=True)
    _, staged_json = _stage_a_create(settings)
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


def test_the_dlq_view_lists_a_staged_create_as_parseable(tmp_path: Path) -> None:
    settings = _settings(tmp_path, fga_enabled=False)
    key, staged_json = _stage_a_create(settings)

    backlog = asyncio.run(dlq.list_dlq(settings, None, fga_deps.DatasetFilter(_request(), settings, None), limit=100))

    assert [(e.run_id, e.parseable, e.outputs) for e in backlog.events] == [(key, True, [_TABLE])], (
        f"the view shows a committed create as poison: {backlog.events}"
    )
    assert backlog.events[0].event_time == json.loads(staged_json)["eventTime"], "the drawer shows '-' for when the create happened"


def test_the_dlq_replay_re_ingests_a_staged_create_through_the_dataset_door(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, fga_enabled=False)
    _arm_signing(monkeypatch)
    key, _ = _stage_a_create(settings)
    repo = _Repo()

    out = asyncio.run(dlq.replay_dlq(key, _request(), cast("Any", repo), settings, None, fga_deps.DatasetFilter(_request(), settings, None), None))

    assert out.status == "replayed"
    assert [e.dataset.name for e in repo.datasets] == [_TABLE] and repo.runs == [], "the replay did not reach the dataset door"
    assert outbox.resolve_event(settings.outbox_uri, {}, key) is None, "a replayed event must be dropped"


def test_the_dlq_replay_refuses_a_create_the_operator_may_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, fga_enabled=True)
    _arm_signing(monkeypatch)
    key, _ = _stage_a_create(settings)

    async def see_not_write(_client: object, *, relation: str, objects: list[str], **_kw: object) -> dict[str, bool]:
        return dict.fromkeys(objects, relation == "can_get_metadata")

    monkeypatch.setattr(fga, "batch_check", see_not_write)
    operator = IDToken(iss="i", sub="operator", aud="lance", exp=0, iat=0)
    repo, publisher = _Repo(), _Publisher()

    with pytest.raises(PermissionDeniedError):
        asyncio.run(dlq.replay_dlq(key, _request(), cast("Any", repo), settings, operator, fga_deps.DatasetFilter(_request(), settings, operator), publisher))

    assert repo.datasets == [] and repo.runs == [] and publisher.published == [], "a refused replay reached the graph or the bus"
    assert outbox.resolve_event(settings.outbox_uri, {}, key) is not None, "a refused replay must leave the object staged"


def test_the_dlq_replay_re_publishes_the_staged_bytes_to_the_lineage_topic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The relay's manual twin tells the SUBSCRIBERS too: a replayed create reaches the notifications bus lane only this way."""
    settings = _settings(tmp_path, fga_enabled=False)
    _arm_signing(monkeypatch)
    key, staged_json = _stage_a_create(settings)
    publisher = _Publisher()

    out = asyncio.run(dlq.replay_dlq(key, _request(), cast("Any", _Repo()), settings, None, fga_deps.DatasetFilter(_request(), settings, None), publisher))

    assert out.status == "replayed"
    assert publisher.published == [staged_json], "subscribers never hear of a replayed create; the re-publish must carry the staged bytes"
    assert publisher.topics == [(settings.dapr_pubsub, settings.dapr_topic)], "the replay announced the create somewhere other than the lineage topic"
    assert outbox.resolve_event(settings.outbox_uri, {}, key) is None, "a replayed event must be dropped"


def test_a_replay_whose_publish_fails_leaves_the_object_for_the_relay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, fga_enabled=False)
    _arm_signing(monkeypatch)
    key, _ = _stage_a_create(settings)

    with pytest.raises(ServiceUnavailableError, match="re-announcing it failed"):
        asyncio.run(dlq.replay_dlq(key, _request(), cast("Any", _Repo()), settings, None, fga_deps.DatasetFilter(_request(), settings, None), _SidecarDown()))

    assert outbox.resolve_event(settings.outbox_uri, {}, key) is not None, "the drop destroyed the copy the relay would still have re-published"


@pytest.mark.parametrize("body", _HOSTILE.values(), ids=_HOSTILE.keys())
def test_the_dlq_view_lists_bytes_no_parser_accepts_as_poison(tmp_path: Path, body: str) -> None:
    settings = _settings(tmp_path, fga_enabled=False)
    key, _ = _stage_a_create(settings)
    hostile = _stage_hostile_first(settings, body)

    backlog = asyncio.run(dlq.list_dlq(settings, None, fga_deps.DatasetFilter(_request(), settings, None), limit=100))

    assert [(e.run_id, e.parseable) for e in backlog.events] == [(hostile, False), (key, True)], f"the view does not show the poison: {backlog.events}"


@pytest.mark.parametrize(
    ("fga_enabled", "answer"), [(False, UnsupportedOperationError), (True, TransactionNotFoundError)], ids=["auth-off-422", "governed-404"]
)
@pytest.mark.parametrize("body", _HOSTILE.values(), ids=_HOSTILE.keys())
def test_the_dlq_replay_answers_bytes_no_parser_accepts_as_poison(tmp_path: Path, body: str, fga_enabled: bool, answer: type[Exception]) -> None:
    settings = _settings(tmp_path, fga_enabled=fga_enabled)
    hostile = _stage_hostile_first(settings, body)
    flt = fga_deps.DatasetFilter(_request(), settings, None)

    with pytest.raises(answer):
        asyncio.run(dlq.replay_dlq(hostile, _request(), cast("Any", _Repo()), settings, None, flt, _Publisher()))

    assert outbox.resolve_event(settings.outbox_uri, {}, hostile) is not None, "a poison replay must leave the object for the relay"


def test_the_dlq_replay_refuses_a_create_retargeted_after_signing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The replay runs the relay's gate over the staged bytes: an operator cannot replay what the relay refuses."""
    settings = _settings(tmp_path, fga_enabled=False)
    key, staged_json = _stage_a_create(settings)
    _rewrite_staged(settings, key, _retargeted(staged_json))
    _arm_signing(monkeypatch)
    repo, publisher = _Repo(), _Publisher()

    with pytest.raises(PermissionDeniedError, match="does not verify"):
        asyncio.run(dlq.replay_dlq(key, _request(), cast("Any", repo), settings, None, fga_deps.DatasetFilter(_request(), settings, None), publisher))

    assert repo.datasets == [] and publisher.published == [], "a tampered create was replayed into the graph or onto the bus"
    assert outbox.resolve_event(settings.outbox_uri, {}, key) is not None


def test_the_dlq_replay_refuses_an_unsigned_create_its_stamped_author_may_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stamped author is authorized as well as the operator: an operator who may write the table
    still cannot replay provenance stamped with a subject who may not."""
    settings = _settings(tmp_path, fga_enabled=True)
    key, staged_json = _stage_a_create(settings)
    _rewrite_staged(settings, key, _unsigned(staged_json))
    _arm_gate(monkeypatch, writer="operator")
    operator = IDToken(iss="i", sub="operator", aud="lance", exp=0, iat=0)
    repo, publisher = _Repo(), _Publisher()

    with pytest.raises(PermissionDeniedError):
        asyncio.run(dlq.replay_dlq(key, _request(), cast("Any", repo), settings, operator, fga_deps.DatasetFilter(_request(), settings, operator), publisher))

    assert repo.datasets == [] and publisher.published == []


def test_a_create_for_a_table_the_operator_cannot_see_is_neither_listed_nor_replayable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The view hides it, and the replay answers exactly as for an absent key: no 403 naming the table."""
    settings = _settings(tmp_path, fga_enabled=True)
    key, _ = _stage_a_create(settings)
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


def test_the_dlq_replay_re_publishes_a_run_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    staged_json = json.dumps(event)
    outbox.stage_event(settings.outbox_uri, {}, event["run"]["runId"], staged_json)
    [(key, _)] = list(outbox.list_events(settings.outbox_uri, {}))
    publisher = _Publisher()

    out = asyncio.run(dlq.replay_dlq(key, _request(), cast("Any", _Repo()), settings, None, fga_deps.DatasetFilter(_request(), settings, None), publisher))

    assert out.status == "replayed"
    assert publisher.published == [staged_json], "a replayed head run never reaches /bronze-arrival"


def test_a_replay_whose_re_announce_fails_is_audited_as_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, fga_enabled=False)
    key, _ = _stage_a_create(settings)
    _arm_signing(monkeypatch)
    audited: list[tuple[str, str, object]] = []
    monkeypatch.setattr(dlq.audit, "audit", lambda action, outcome, **kw: audited.append((action, outcome, kw.get("resource"))))

    with pytest.raises(ServiceUnavailableError):
        asyncio.run(dlq.replay_dlq(key, _request(), cast("Any", _Repo()), settings, None, fga_deps.DatasetFilter(_request(), settings, None), _SidecarDown()))

    # A static change names no run, so the record names the table it changed.
    assert audited == [("dlq_replay", "failure", f"table:{_TABLE}")], f"a replay that changed the graph left no true audit record: {audited}"
