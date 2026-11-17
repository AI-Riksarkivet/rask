"""The lanes vectors of ``gen_port_vectors.py``: what lineage runs without a request, put to the real Python.

Five groups. ``settings``: environments read by the real ``LineageSettings``, each accepted with the values it gives or
refused naming its variable. ``turns``: turns of the real ``lakehouse.timers.Timer`` with the claim's read and write
answered by stand-ins (the store's clock, the claim found, whether the write lands, whether the work fails). ``ticks``:
the real ``lineage.api.relay.tick`` over stand-ins of the outbox, the gate, the store, the bus and the forwarder that
note each call, so a vector is the order of the effects and the report. ``forwards``: the real
``lineage.services.forward.forward`` over a stand-in feed and backend: what is sent, how the cursor moves. ``parks``:
what the real ``IngestLane.park`` keeps of a delivery, and under which key.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch


_RUN = {"eventType": "COMPLETE", "eventTime": "2026-11-17T10:00:00Z", "job": {"namespace": "j", "name": "n"}}
_STORE = {"LINEAGE_S3_WEB_IDENTITY_TOKEN_FILE": "/var/run/secrets/rask/store/token", "LINEAGE_S3_STS_ENDPOINT": "http://sts.store:9000"}


# --- settings -------------------------------------------------------------------------------------------------------

_SETTINGS: dict[str, str] = {
    "catalog_url": "LINEAGE_CATALOG_URL",
    "catalog_identity_token_file": "RASK_CATALOG_IDENTITY_TOKEN_FILE",
    "provenance_namespace": "LINEAGE_PROVENANCE_NAMESPACE",
    "catalog_dataset_namespace": "LINEAGE_CATALOG_DATASET_NAMESPACE",
    "catalog_timeout_seconds": "LINEAGE_CATALOG_TIMEOUT_SECONDS",
    "forward_url": "LINEAGE_FORWARD_URL",
    "forward_endpoint": "LINEAGE_FORWARD_ENDPOINT",
    "forward_api_key_secret": "LINEAGE_FORWARD_API_KEY_SECRET",
    "forward_api_key_field": "LINEAGE_FORWARD_API_KEY_FIELD",
    "forward_batch": "LINEAGE_FORWARD_BATCH",
    "forward_timeout_seconds": "LINEAGE_FORWARD_TIMEOUT_SECONDS",
    "events_topic": "LINEAGE_EVENTS_TOPIC",
    "deliveries_in_flight": "LINEAGE_DELIVERIES_IN_FLIGHT",
    "delivery_attempts": "LINEAGE_DELIVERY_ATTEMPTS",
    "delivery_retry_seconds": "LINEAGE_DELIVERY_RETRY_SECONDS",
    "delivery_ack_wait_seconds": "LINEAGE_DELIVERY_ACK_WAIT_SECONDS",
    "publish_timeout_seconds": "LINEAGE_PUBLISH_TIMEOUT_SECONDS",
    "relay_period_seconds": "LINEAGE_RELAY_PERIOD_SECONDS",
    "timer_root": "LINEAGE_TIMER_ROOT",
    "read_only": "LINEAGE_READ_ONLY",
    "outbox_uri": "LINEAGE_OUTBOX_URI",
    "outbox_drain_limit": "LINEAGE_OUTBOX_DRAIN_LIMIT",
    "events_retention_days": "LINEAGE_EVENTS_RETENTION_DAYS",
    "audit_enabled": "LANCE_AUDIT_ENABLED",
    "docs_enabled": "LINEAGE_DOCS",
    "max_body_bytes": "LINEAGE_MAX_BODY_BYTES",
    "request_deadline_seconds": "LINEAGE_REQUEST_DEADLINE_SECONDS",
}
_SIGNING = {"signers": "LINEAGE_SIGNERS", "delegators": "LINEAGE_DELEGATORS"}


def _environments() -> list[dict[str, str]]:
    """Every value the chart renders, then one variable at a time: the forms pydantic's lax mode takes, and each refusal."""
    chart = {
        "LINEAGE_CATALOG_URL": "http://rask-catalog:8000",
        "RASK_CATALOG_IDENTITY_TOKEN_FILE": "/var/run/secrets/rask/identity/rask-catalog/token",
        "LINEAGE_PROVENANCE_NAMESPACE": "lance_catalog$provenance",
        "LINEAGE_CATALOG_DATASET_NAMESPACE": "lance-namespace://rask.example/api/catalog",
        "LINEAGE_DOCS": "false",
        "LINEAGE_MAX_BODY_BYTES": "8388608",
        "LINEAGE_REQUEST_DEADLINE_SECONDS": "300",
        "LINEAGE_READ_ONLY": "false",
        "LINEAGE_EVENTS_RETENTION_DAYS": "7",
        "LINEAGE_FORWARD_URL": "http://marquez:5000/",
        "LINEAGE_FORWARD_ENDPOINT": "/api/v1/lineage",
        "LINEAGE_FORWARD_API_KEY_SECRET": "lineage-forward",
        "LINEAGE_FORWARD_API_KEY_FIELD": "api-key",
        "LINEAGE_EVENTS_TOPIC": "lineage.events.v1",
        "LINEAGE_DELIVERIES_IN_FLIGHT": "64",
        "LINEAGE_DELIVERY_ATTEMPTS": "5",
        "LINEAGE_DELIVERY_RETRY_SECONDS": "120",
        "LINEAGE_DELIVERY_ACK_WAIT_SECONDS": "300",
        "LINEAGE_RELAY_PERIOD_SECONDS": "30",
        "LINEAGE_TIMER_ROOT": "s3://rask-control",
        "LINEAGE_OUTBOX_URI": "s3://rask-control/_lineage_outbox",
        "LANCE_AUDIT_ENABLED": "true",
        "LINEAGE_SIGNERS": '["system:serviceaccount:rask:rask-sa-catalog", "system:serviceaccount:rask:rask-sa-maintenance"]',
        "LINEAGE_DELEGATORS": '["system:serviceaccount:rask:rask-sa-catalog"]',
        **_STORE,
    }
    one: list[tuple[str, str]] = [("LINEAGE_DELIVERY_ATTEMPTS", value) for value in ("5.0", " 7 ", "+5", "1_000", "0", "-3", "5.5", "0x5", "", "five", "1__0")]
    one += [("lineage_delivery_attempts", "3"), ("Lineage_Read_Only", "yes")]
    one += [("LINEAGE_READ_ONLY", value) for value in ("Yes", "off", "1", "0", "TRUE", "", "maybe", "2")]
    one += [("LINEAGE_DOCS", "on"), ("LANCE_AUDIT_ENABLED", "f")]
    one += [("LINEAGE_DELIVERY_RETRY_SECONDS", value) for value in ("1e2", " 2.5 ", "0", "-1", "nan", "x", "+3")]
    one += [("LINEAGE_DELIVERY_ACK_WAIT_SECONDS", value) for value in ("10", "10.0", "10.5", "11")]
    one += [("LINEAGE_PUBLISH_TIMEOUT_SECONDS", value) for value in ("0.05", "0.1", "2")]
    one += [("LINEAGE_RELAY_PERIOD_SECONDS", value) for value in ("-1", "0")]
    one += [("LINEAGE_FORWARD_BATCH", value) for value in ("0", "1", "")]
    one += [("LINEAGE_DELIVERIES_IN_FLIGHT", value) for value in ("0", "8")]
    one += [("LINEAGE_EVENTS_RETENTION_DAYS", value) for value in ("-1", "0", "30")]
    one += [("LINEAGE_MAX_BODY_BYTES", value) for value in ("0", "1024")]
    one += [("LINEAGE_REQUEST_DEADLINE_SECONDS", value) for value in ("0", "0.5")]
    one += [("LINEAGE_CATALOG_TIMEOUT_SECONDS", value) for value in ("0", "2")]
    one += [("LINEAGE_FORWARD_TIMEOUT_SECONDS", value) for value in ("-2", "3.5")]
    one += [("LINEAGE_OUTBOX_DRAIN_LIMIT", value) for value in ("0", "10", "x")]
    one += [("LINEAGE_CATALOG_DATASET_NAMESPACE", value) for value in ("x", "lance-namespace://", "lance-namespace://h", "s3://b")]
    one += [("LINEAGE_SIGNERS", value) for value in ("a", "[1]", '{"a": 1}', "[]", '["a", "a"]', '"a"')]
    one += [("LINEAGE_DELEGATORS", '["b"]'), ("LINEAGE_EVENTS_TOPIC", "lineage.other.v1"), ("LINEAGE_PROVENANCE_NAMESPACE", "p$q")]
    environments: list[dict[str, str]] = [{}, chart]
    environments += [{name: value} for name, value in one]
    # The relay claims its windows on the store, so a period needs a root, and a root on the store an identity there.
    environments += [
        {"LINEAGE_RELAY_PERIOD_SECONDS": "30"},
        {"LINEAGE_RELAY_PERIOD_SECONDS": "7.5", "LINEAGE_TIMER_ROOT": "s3://rask-control", **_STORE},
        {"LINEAGE_OUTBOX_URI": "s3://rask-control/_lineage_outbox", **_STORE},
        {"LINEAGE_OUTBOX_URI": "s3://rask-control/_lineage_outbox", **_STORE, "LINEAGE_S3_ACCESS_KEY_ID": "AKIA"},
    ]
    return environments


def _refused(exc: Exception) -> list[str]:
    """The variables a refusal names: each error's alias, else the names its message spells out."""
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        named = [str(error["loc"][0]) for error in exc.errors() if error["loc"]]
        return named or sorted(set(re.findall(r"LINEAGE_[A-Z0-9_]+", str(exc))))[:1]
    field = re.search(r'field "([a-z_]+)"', str(exc))
    return [_SIGNING.get(field.group(1), _SETTINGS.get(field.group(1), "?")) if field else "?"]


def _settings(env: dict[str, str]) -> dict[str, Any]:
    from lineage.core.config import LineageSettings

    with patch.dict(os.environ, env, clear=True):
        try:
            settings = LineageSettings()
        except Exception as exc:
            return {"env": env, "refused": _refused(exc)}
    values: dict[str, Any] = {field: getattr(settings, field) for field in _SETTINGS}
    values |= {"signers": sorted(settings.signing.signers), "delegators": sorted(settings.signing.delegators)}
    values |= {"on_the_store": settings.signs_store_requests}
    return {"env": env, "accepted": values}


# --- timer turns ----------------------------------------------------------------------------------------------------


class _Denied(Exception):
    """The store refused a read as forbidden."""


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _claim(window: str, at: str = "2026-11-17T09:59:00Z", by: str = "replica-b") -> str:
    return json.dumps({"timer": "lineage-relay", "window": window, "claimed_at": at, "by": by})


def _turn_cases() -> list[dict[str, Any]]:
    """A turn's inputs: the store's clock, the period, what the read finds, how the write ends, and the work."""
    at = "2026-11-17T10:00:07.250000+00:00"

    def case(found: str, *, record: str | None = None, put: str = "lands", work: str = "ok", period: float = 30.0, when: str = at) -> dict[str, Any]:
        return {"at": when, "period": period, "found": found, "record": record, "put": put, "work": work, "held": False}

    cases = [
        case("absent"),
        case("absent", work="fails"),
        case("absent", put="lost"),
        case("absent", put="fails"),
        case("absent", period=7.5),
        case("absent", period=3600.0, when="2026-11-17T10:59:59.999999+00:00"),
        case("absent", period=0.25, when="2026-11-17T10:00:00.130000+00:00"),
        case("absent", when="2026-11-17T10:00:00+00:00"),
        case("held", record=_claim("2026-11-17T10:00:00Z")),
        case("held", record=_claim("2026-11-17T09:59:30Z")),
        case("held", record=_claim("2026-11-17T09:59:30Z"), put="lost"),
        case("held", record=_claim("2026-11-17T10:00:30Z")),
        case("held", record=_claim("2026-11-17T10:00:37.250000Z")),
        case("held", record=_claim("2026-11-17T10:00:37.250001Z")),
        case("held", record=_claim("2026-11-17T10:05:00Z")),
        case("held", record=_claim("2026-11-17T11:00:00+01:00")),
        case("held", record=json.dumps({"timer": "lineage-relay"})),
        case("held", record=_claim("2026-11-17T10:00:00")),
        case("held", record="not json"),
        case("denied"),
        case("denied", put="lost"),
        case("fails"),
        case("fails", period=5.0),
    ]
    return [*cases, {**case("absent"), "held": True}, {**case("absent", period=5.0), "held": True}]


async def _turned(case: dict[str, Any]) -> dict[str, Any]:
    from lakehouse import records, timers

    at = _at(case["at"])
    writes: list[dict[str, Any]] = []
    worked: list[bool] = []

    def read_json_at(_root: str, _options: object, key: str) -> records.StoreRead:
        assert key == "_timers/lineage-relay.json"
        if case["found"] == "fails":
            raise ConnectionError("the store did not answer")
        if case["found"] == "denied":
            raise _Denied("AccessDenied")
        if case["found"] == "absent":
            return records.StoreRead(stored=None, at=at)
        stored = records.Stored(record=json.loads(case["record"]), etag="etag-1", modified=at)
        return records.StoreRead(stored=stored, at=at)

    def write(unless: str) -> Callable[..., None]:
        def written(_root: str, _options: object, key: str, claim: dict[str, Any], **etag: str) -> None:
            writes.append({"key": key, "unless": etag.get("etag", unless), "body": json.dumps(claim)})
            if case["put"] == "lost":
                raise records.RecordChangedError("changed") if etag else records.RecordExistsError("exists")
            if case["put"] == "fails":
                raise ConnectionError("the store did not answer")

        return written

    async def work() -> None:
        worked.append(True)
        if case["work"] == "fails":
            raise RuntimeError("the tick failed")

    timer = timers.Timer(
        "lineage-relay", period=case["period"], work=work, root="s3://b", storage_options=lambda: cast("Any", {}), held=lambda: case["held"], holder="replica-a"
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(timers.records, "read_json_at", read_json_at))
        stack.enter_context(patch.object(timers.records, "create_json", write("present")))
        stack.enter_context(patch.object(timers.records, "replace_json", write("changed")))
        stack.enter_context(patch.object(timers, "_denied_at", lambda exc: at if isinstance(exc, _Denied) else None))
        stack.enter_context(patch.object(timers, "monotonic", lambda: 0.0))
        turn = await timer.turn()
    window = None if turn.window is None else turn.window.isoformat()
    return case | {"outcome": turn.outcome.value, "window": window, "wait": turn.wait, "writes": writes, "worked": bool(worked)}


# --- relay ticks ----------------------------------------------------------------------------------------------------

#: What each staged event does when it is drained, by the word its key starts with.
_KINDS = ("drained", "unavailable", "unrecorded", "unpublished", "undropped", "denied", "unkept", "foreign", "unverified", "poison")


def _tick_cases() -> list[dict[str, Any]]:
    every = [f"{kind}-{index}" for index, kind in enumerate(_KINDS)]

    def case(**members: Any) -> dict[str, Any]:  # noqa: ANN401 - a case is arbitrary JSON
        base = {"outbox": True, "listing": "ok", "staged": [], "limit": 500, "retire": 2, "forward": None, "retention": 0, "cursor": 12, "prune": 4}
        return base | members

    return [
        case(),
        case(outbox=False),
        case(staged=every),
        case(staged=every, limit=0),
        case(staged=every[:3], limit=2),
        case(staged=["drained-0", "poison-1"], listing="fails"),
        case(retire="fails"),
        case(forward={"forwarded": 3, "refused": 1, "stalled": None}),
        case(forward={"forwarded": 1, "refused": 0, "stalled": "HTTP 503"}),
        case(forward="raises"),
        case(retention=7),
        case(retention=7, prune="fails"),
        case(retention=7, forward={"forwarded": 0, "refused": 0, "stalled": None}),
        case(retention=7, forward={"forwarded": 0, "refused": 0, "stalled": None}, cursor="fails"),
        case(retention=30, forward="raises", staged=every[:6], retire=0, prune=0),
    ]


def _staged_text(key: str) -> str:
    from lakehouse.cloud_events import lineage_envelope

    if key.startswith("poison"):
        return "not json"
    return json.dumps(lineage_envelope(_RUN | {"run": {"runId": key}}).as_wire())


def _kind(name: str) -> str:
    """What a staged event does when drained: the word its run id, or its key, starts with."""
    return name.split("-")[0]


class _Tick:
    """Stand-ins for everything a tick reaches, each noting that it was called and answering what the case says."""

    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case
        self.effects: list[str] = []
        self.counted: list[str] = []

    def _note(self, effect: str, *, fails: bool = False) -> None:
        self.effects.append(effect)
        if fails:
            raise ConnectionError(f"{effect} did not answer")

    def measured(self, *_: object) -> tuple[int, float]:
        self._note("list", fails=self.case["listing"] == "fails")
        return len(self.case["staged"]), 0.0

    def listed(self, *_: object, limit: int | None = None) -> Iterator[tuple[str, str]]:
        return iter([(key, _staged_text(key)) for key in self.case["staged"][:limit]])

    def drop(self, _uri: str, _options: object, key: str) -> None:
        self._note(f"drop {key}", fails=_kind(key) == "undropped")

    def count(self, outcome: Any, *, door: Any) -> None:  # noqa: ANN401 - the metrics' enums
        self.counted.append(f"{door.value} {outcome.value}")

    async def authorize(self, event: Any, *_: object) -> frozenset[str]:  # noqa: ANN401 - the parsed event
        from lance_namespace import PermissionDeniedError

        from lineage.models import UnverifiedEventError

        self._note("authorize", fails=_kind(event.run_id) == "unavailable")
        if _kind(event.run_id) in {"denied", "unkept"}:
            raise PermissionDeniedError("can_write_data required on outputs")
        if _kind(event.run_id) == "unverified":
            raise UnverifiedEventError("no listed signer verifies it", "unsigned")
        return frozenset()

    async def record(self, event: Any, **_: object) -> None:  # noqa: ANN401 - the parsed event
        from lineage.models import ForeignRunError

        self._note("record", fails=_kind(event.run_id) == "unrecorded")
        if _kind(event.run_id) == "foreign":
            raise ForeignRunError("run was founded by another subject")

    async def record_refusal(self, *, outbox_key: str, **_: object) -> None:
        self._note(f"keep {outbox_key}", fails=_kind(outbox_key) == "unkept")

    async def retire_recovered_parks(self) -> int:
        self._note("retire", fails=self.case["retire"] == "fails")
        return int(self.case["retire"])

    async def feed_incarnation(self) -> str:
        return "feed-1"

    async def cursor(self, _name: str) -> int:
        self._note("cursor", fails=self.case["cursor"] == "fails")
        return int(self.case["cursor"])

    async def prune_events(self, days: int, *, delivered_through: int | None) -> int:
        self._note(f"prune {days} {delivered_through}", fails=self.case["prune"] == "fails")
        return int(self.case["prune"])

    async def republish(self, _publisher: object, _settings: object, staged: Any) -> None:  # noqa: ANN401 - the staged envelope
        self._note("republish", fails=_kind(staged.data["run"]["runId"]) == "unpublished")

    async def forward(self, *_: object) -> Any:  # noqa: ANN401 - the forwarder's outcome
        from lineage.services.forward import ForwardOutcome

        self._note("forward", fails=self.case["forward"] == "raises")
        return ForwardOutcome.model_validate(self.case["forward"])


async def _ticked(case: dict[str, Any]) -> dict[str, Any]:
    from lineage.api import relay

    tick = _Tick(case)
    settings: Any = SimpleNamespace(
        outbox_uri="s3://b/_lineage_outbox" if case["outbox"] else "",
        outbox_drain_limit=case["limit"],
        forward_url="http://graph" if case["forward"] is not None else "",
        forward_endpoint="api/v1/lineage",
        events_retention_days=case["retention"],
    )
    app: Any = SimpleNamespace(state=SimpleNamespace(provenance=tick, bus=None, forward_client=object()))
    with ExitStack() as stack:
        stack.enter_context(patch.object(relay.outbox, "measured_backlog", tick.measured))
        stack.enter_context(patch.object(relay.outbox, "list_events", tick.listed))
        stack.enter_context(patch.object(relay.outbox, "drop_event", tick.drop))
        stack.enter_context(patch.object(relay, "storage_options", lambda _settings: {}))
        stack.enter_context(patch.object(relay, "enforce_bus_authz", tick.authorize))
        stack.enter_context(patch.object(relay, "republish_staged", tick.republish))
        stack.enter_context(patch.object(relay, "forward", tick.forward))
        stack.enter_context(patch.object(relay, "record_outcome", tick.count))
        report = await relay.tick(app, settings)
    texts = {key: _staged_text(key) for key in case["staged"]}
    return case | {"texts": texts, "effects": tick.effects, "report": report.model_dump(), "counted": tick.counted}


# --- forwards -------------------------------------------------------------------------------------------------------


def _forward_cases() -> list[dict[str, Any]]:
    def case(mark: int, seqs: list[int], **members: Any) -> dict[str, Any]:  # noqa: ANN401 - a case is arbitrary JSON
        return {"mark": mark, "seqs": seqs, "answers": {}, "batch": 100, "authorization": "none"} | members

    six = list(range(1, 7))
    return [
        case(0, []),
        case(0, six),
        case(3, six),
        case(6, six),
        case(9, six),
        case(9, [4, 5, 6]),
        case(1, [4, 5, 6]),
        case(0, six, batch=2),
        case(0, six, answers={"3": 422, "5": 400}),
        case(0, six, answers={"4": 503}),
        case(0, six, answers={"1": 401}),
        case(2, six, answers={"4": "transport"}),
        case(0, six, answers={"2": 413, "3": 302}),
        case(0, six, authorization="bearer"),
        case(0, six, authorization="fails"),
        case(0, [], authorization="fails"),
        case(0, list(range(1, 41))),
        case(5, list(range(1, 41)), batch=33, answers={"30": 422}),
        case(0, [2, 3, 7, 8]),
    ]


class _Window:
    """The feed after ``since``, oldest first, and its bounds: what ``ProvenanceStore.window`` hands the forwarder."""

    def __init__(self, seqs: list[int], since: int) -> None:
        self._seqs = seqs
        self._next = [seq for seq in seqs if seq > since]

    async def bounds(self) -> tuple[int, int] | None:
        return (min(self._seqs), max(self._seqs)) if self._seqs else None

    async def take(self, n: int) -> list[Any]:
        from lineage.schemas import EventRecord

        taken, self._next = self._next[:n], self._next[n:]
        return [EventRecord(seq=seq, event={"seq": seq}) for seq in taken]


async def _forwarded(case: dict[str, Any]) -> dict[str, Any]:
    import httpx

    from lineage.services import forward

    calls: list[str] = []

    class Feed:
        async def feed_incarnation(self) -> str:
            return "feed-1"

        async def cursor(self, name: str) -> int:
            calls.append(f"cursor {name}")
            return int(case["mark"])

        async def window(self, *, since: int) -> _Window:
            calls.append(f"window {since}")
            return _Window(case["seqs"], since)

        async def reset_cursor(self, name: str, seq: int) -> None:
            calls.append(f"reset {name} {seq}")

        async def advance_cursor(self, name: str, seq: int) -> None:
            calls.append(f"advance {name} {seq}")

    class Client:
        async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
            seq = json["seq"]
            calls.append(f"post {url} {seq} {headers.get('Authorization')}")
            answer = case["answers"].get(str(seq), 200)
            if answer == "transport":
                raise httpx.ConnectError("the backend did not answer")
            return httpx.Response(answer, text="")

    async def authorization(_settings: object) -> dict[str, str]:
        calls.append("authorization")
        if case["authorization"] == "fails":
            raise RuntimeError("the secret store did not serve the key")
        return {"Authorization": "Bearer k"} if case["authorization"] == "bearer" else {}

    settings: Any = SimpleNamespace(forward_url="http://graph/", forward_endpoint="/api/v1/lineage", forward_batch=case["batch"])
    feed: Any = Feed()
    client: Any = Client()
    with patch.object(forward, "_authorization", authorization):
        try:
            outcome: dict[str, Any] = (await forward.forward(feed, settings, client)).model_dump()
        except RuntimeError:
            outcome = {"raised": True}
    return case | {"calls": calls, "outcome": outcome}


# --- parks ----------------------------------------------------------------------------------------------------------


def _park_cases() -> list[dict[str, Any]]:
    from lakehouse.cloud_events import lineage_envelope

    authored = _RUN | {"run": {"runId": "run-1", "facets": {"rask_author": {"sub": "dex~alice", "name": "Alice"}}}}
    wire = lineage_envelope(authored).as_wire()
    bodies: list[tuple[str, Any]] = [
        ("an authored run", wire),
        ("no id", {key: value for key, value in wire.items() if key != "id"}),
        ("an id past the bound", wire | {"id": "x" * 513}),
        ("an id at the bound", wire | {"id": "y" * 512}),
        ("an id that is no string", wire | {"id": 5}),
        ("an empty id", wire | {"id": ""}),
        ("an id of other scripts", wire | {"id": "å-ü-✓"}),
        ("data that is no event, with an id", wire | {"data": {"eventTime": "t"}}),
        ("data that is no event, no id", {"data": {"eventTime": "t"}}),
        ("a run id of spaces", wire | {"data": authored | {"run": {"runId": "   "}}}),
        ("an author that is no string", wire | {"data": authored | {"run": {"runId": "r", "facets": {"rask_author": {"sub": 7}}}}}),
        ("numbers and text", wire | {"extra": [1.0, 1e20, 12345678901234567890, -0.5, "tab\there", None, True]}),
    ]
    cases = [{"name": name, "body": json.dumps(body, ensure_ascii=False), "recorded": False, "keep": "lands"} for name, body in bodies]
    cases += [{"name": "already recorded", "body": json.dumps(wire), "recorded": True, "keep": "lands"}]
    cases += [{"name": "the keep fails", "body": json.dumps(wire), "recorded": False, "keep": "fails"}]
    return [
        *cases,
        {"name": "no envelope", "body": "not json", "recorded": False, "keep": "lands"},
        {"name": "a list", "body": "[1]", "recorded": False, "keep": "lands"},
    ]


async def _parked(case: dict[str, Any]) -> dict[str, Any]:
    from lakehouse.bus_delivery import Delivery
    from lineage.api.ingest_lane import IngestLane

    asked: list[list[str]] = []
    kept: list[dict[str, Any]] = []
    counted: list[str] = []

    class Store:
        async def events_recorded(self, keys: set[str]) -> set[str]:
            asked.append(sorted(keys))
            return set(keys) if case["recorded"] else set()

        async def record_refusal(self, **kwargs: Any) -> None:  # noqa: ANN401 - the row's members
            kept.append(kwargs)
            if case["keep"] == "fails":
                raise ConnectionError("the refusals table did not answer")

    app: Any = SimpleNamespace(state=SimpleNamespace(provenance=Store()))
    settings: Any = SimpleNamespace()
    lane = IngestLane(app, settings)
    delivery = Delivery(subject="lineage.events.v1", body=case["body"].encode(), sequence=1, attempt=5)
    with patch("lineage.api.ingest_lane.record_outcome", lambda outcome, *, door: counted.append(f"{door.value} {outcome.value}")):
        try:
            await lane.park(delivery, "its handling asked for it to be parked")
            raised = False
        except ConnectionError:
            raised = True
    return case | {"asked": asked, "kept": kept, "raised": raised, "counted": counted}


def lanes_vectors() -> dict[str, list[Any]]:
    """Each group's cases with what the Python answered for them."""

    async def every() -> dict[str, list[Any]]:
        return {
            "turns": [await _turned(case) for case in _turn_cases()],
            "ticks": [await _ticked(case) for case in _tick_cases()],
            "forwards": [await _forwarded(case) for case in _forward_cases()],
            "parks": [await _parked(case) for case in _park_cases()],
        }

    return {"settings": [_settings(env) for env in _environments()], **asyncio.run(every())}
