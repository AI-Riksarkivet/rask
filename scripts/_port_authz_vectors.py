"""The authorization vectors of ``gen_port_vectors.py``: which questions lineage's gates ask, and what each answer leads to.

Every case is put to the real functions of ``lineage.api.fga_deps`` and ``lineage.services.names``. Only what they ask
is replaced: the decider answers from the case and records each question in the order it is asked, the feed answers a
run's author and whether the event is already recorded, and the catalog's describe doors answer from the case. The
signature verdict is the case's too, since ``signing.json`` holds its own vectors. The catalog decider is recorded over
httpx's mock transport, so its questions are the requests it sends.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


_CATALOG = "lance-namespace://localhost/api/catalog"
_OTHER_CATALOG = "lance-namespace://elsewhere/api/catalog"
_ROOT = "estate:rask"
_ADA = "dex~ada"
_BOB = "dex~bob"
_UA = "0190a1b2-0000-7000-8000-0000000000a1"
_UB = "0190a1b2-0000-7000-8000-0000000000b2"
_UC = "0190a1b2-0000-7000-8000-0000000000c3"
_UD = "0190a1b2-0000-7000-8000-0000000000d4"
_UNS = "0190a1b2-0000-7000-8000-0000000000ff"
_T = "2026-11-17T10:00:00Z"
_LOCATION = "s3://lake/wh/raw/pages.lance"
_CATALOG_ID = "catalog"

Answers = dict[str, Any]


def _obj(uuid: str) -> str:
    return f"table:{uuid}"


def _table(name: str, uuid: str | None = None, **facets: Any) -> dict[str, Any]:  # noqa: ANN401 - a facet is arbitrary JSON
    lance = {"rask_lance": {"tableUuid": uuid}} if uuid else {}
    return {"namespace": _CATALOG, "name": name, "facets": lance | facets}


def _located(namespace: str = "s3://lake", name: str = "wh/raw/pages.lance", **facets: Any) -> dict[str, Any]:  # noqa: ANN401 - a facet is arbitrary JSON
    return {"namespace": namespace, "name": name, "facets": facets}


def _symlinks(*identifiers: tuple[str, str]) -> dict[str, Any]:
    return {"symlinks": {"identifiers": [{"namespace": namespace, "name": name, "type": "TABLE"} for namespace, name in identifiers]}}


def _columns(*upstreams: tuple[str, str]) -> dict[str, Any]:
    fields = [{"namespace": namespace, "name": name, "field": "c"} for namespace, name in upstreams]
    return {"columnLineage": {"fields": {"c": {"inputFields": fields}}}}


def _run(inputs: list[Any] | None = None, outputs: list[Any] | None = None, *, run_id: str = "run-1", **run_facets: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "eventType": "COMPLETE",
        "eventTime": _T,
        "run": {"runId": run_id, "facets": run_facets},
        "job": {"namespace": "jobs", "name": "copy"},
        "inputs": inputs or [],
        "outputs": outputs or [],
    }


def _authored(event: dict[str, Any], sub: str | None) -> dict[str, Any]:
    if sub is None:
        return event
    holder = event["dataset"] if "dataset" in event else event["run"]
    holder["facets"] = holder.get("facets", {}) | {"rask_author": {"sub": sub}}
    return event


def _operated(operation: str, *, uuid: str | None = _UA, name: str = "wh$raw$pages") -> dict[str, Any]:
    """A run of ``operation`` on one table, as the catalog announces a write."""
    return _run([], [_table(name, uuid)], run_id=f"run-{operation}", rask_lance={"operation": operation})


def _dataset_event(operation: str, uuid: str | None = _UA, name: str = "wh$raw$pages") -> dict[str, Any]:
    lance: dict[str, Any] = {"operation": operation} | ({"tableUuid": uuid} if uuid else {})
    return {"eventTime": _T, "producer": "https://catalog", "dataset": {"namespace": _CATALOG, "name": name, "facets": {"rask_lance": lance}}}


class _Decider:
    """A decider answering from ``answers``, recording every question in the order it is asked."""

    def __init__(self, answers: Answers, asked: list[dict[str, Any]]) -> None:
        self._answers = answers
        self._asked = asked

    @property
    def resolves_names(self) -> bool:
        return bool(self._answers.get("resolves_names", True))

    def _ask(self, question: dict[str, Any]) -> None:
        from lance_namespace import ServiceUnavailableError

        self._asked.append(question)
        if question["ask"] in self._answers.get("unavailable", []):
            raise ServiceUnavailableError("authorization service unavailable")

    def _allowed(self, relation: str, obj: str) -> bool:
        return obj in self._answers.get("allowed", {}).get(relation, [])

    async def check(self, *, user: str, relation: str, obj: str) -> bool:
        self._ask({"ask": "check", "user": user, "relation": relation, "objects": [obj]})
        return self._allowed(relation, obj)

    async def batch_check(self, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        # Sorted: a filter asks about a set, whose order Python leaves to the hash seed.
        self._ask({"ask": "batch_check", "user": user, "relation": relation, "objects": sorted(objects)})
        return {obj: self._allowed(relation, obj) for obj in objects}

    async def check_tables(self, *, user: str, relation: str, tables: list[str]) -> dict[str, Any]:
        from lakehouse.governed.decider import TableDecision

        self._ask({"ask": "check_tables", "user": user, "relation": relation, "tables": list(tables)})
        known = self._answers.get("tables", {})
        return {name: TableDecision(allowed=True, **known[name]) if name in known else TableDecision(allowed=False) for name in tables}

    async def governed(self, objects: list[str]) -> dict[str, bool]:
        self._ask({"ask": "governed", "objects": list(objects)})
        return {obj: obj in self._answers.get("governed", []) for obj in objects}

    async def locate(self, locations: list[str]) -> dict[str, str | None]:
        self._ask({"ask": "locate", "locations": list(locations)})
        return {location: self._answers.get("located", {}).get(location) for location in locations}

    async def list_users(self, *, relation: str, obj: str) -> list[str]:
        self._ask({"ask": "list_users", "relation": relation, "objects": [obj]})
        return []

    async def close(self) -> None:
        return None


class _Feed:
    """The feed's two lookups a gate makes: who founded a run, and whether this very event is already recorded."""

    def __init__(self, case: dict[str, Any], asked: list[dict[str, Any]], recorded: object) -> None:
        self._case = case
        self._asked = asked
        self._recorded = recorded

    async def run_author(self, run_id: str) -> str | None:
        self._asked.append({"ask": "run_author", "run_id": run_id})
        return self._case.get("run_author")

    async def recorded_event(self, _: str) -> object:
        self._asked.append({"ask": "recorded"})
        return self._recorded if self._case.get("replay") else None


def _settings(case: dict[str, Any]) -> Any:  # noqa: ANN401 - stands in for LineageSettings
    from lineage.core.config import Signing

    signing = Signing(signers=frozenset(case.get("signers", [])), delegators=frozenset(case.get("delegators", [])))
    return SimpleNamespace(fga_enabled=case.get("fga", True), fga_root_object=_ROOT, signing=signing)


def _app(case: dict[str, Any], asked: list[dict[str, Any]], recorded: object = None) -> Any:  # noqa: ANN401 - stands in for the app
    decider = _Decider(case.get("answers", {}), asked) if case.get("decider", True) else None
    return SimpleNamespace(state=SimpleNamespace(decider=decider, provenance=_Feed(case, asked, recorded)))


def _caller(subject: str | None) -> Any:  # noqa: ANN401 - stands in for a Caller
    return None if subject is None else SimpleNamespace(subject=subject)


async def _outcome(call: Awaitable[Any]) -> dict[str, Any]:
    from lance_namespace import LanceNamespaceError

    try:
        result = await call
    except LanceNamespaceError as exc:
        return {"refused": type(exc).__name__, "message": str(exc)}
    return {"admitted": sorted(result) if result is not None else None}


def _output_cases() -> list[dict[str, Any]]:
    """The HTTP door's question: may the caller record what the event says it read and wrote."""
    a, b, c = _table("wh$raw$pages", _UA), _table("wh$clean$pages", _UB), _table("wh$gold$pages", _UC)
    write, read, maintain = "can_write_data", "can_get_metadata", "can_maintain"
    allow_all = {"allowed": {write: [_obj(_UA), _obj(_UB), _obj(_UC), _obj(_UD)], read: [_obj(_UA), _obj(_UB), _obj(_UC), _obj(_UD)]}}
    maintenance = ["can_write_data", "can_maintain"]
    cases: list[dict[str, Any]] = [
        {"name": "authorization off", "fga": False, "event": _run([a], [b])},
        {"name": "no decider", "decider": False, "event": _run([a], [b])},
        {"name": "no caller", "subject": None, "event": _run([a], [b])},
        {"name": "no caller and nothing named", "subject": None, "event": _run()},
        {"name": "nothing named", "event": _run()},
        {"name": "a run another subject founded", "run_author": _BOB, "event": _run([a], [b]), "answers": allow_all},
        {"name": "a run the caller founded", "run_author": _ADA, "event": _run([a], [b]), "answers": allow_all},
        {"name": "read one, wrote one, both granted", "event": _run([a], [b]), "answers": allow_all},
        {"name": "an output not granted, governed", "event": _run([a], [b]), "answers": {"allowed": {read: [_obj(_UA)]}, "governed": [_obj(_UB)]}},
        {"name": "an output not granted, governed by nothing", "event": _run([a], [b]), "answers": {"allowed": {read: [_obj(_UA)]}}},
        {
            "name": "an output not granted, the governance probe fails",
            "event": _run([a], [b]),
            "answers": {"allowed": {read: [_obj(_UA)]}, "unavailable": ["governed"]},
        },
        {
            "name": "two outputs not granted, one governed",
            "event": _run([], [c, b, a]),
            "answers": {"allowed": {write: [_obj(_UA)]}, "governed": [_obj(_UC)]},
        },
        {"name": "an output with no uuid", "event": _run([a], [b, _table("wh$clean$scans")]), "answers": allow_all},
        {"name": "an output outside the catalog", "event": _run([a], [_located("s3://bucket", "out/x")]), "answers": allow_all},
        {"name": "an output under another catalog", "event": _run([a], [{"namespace": _OTHER_CATALOG, "name": "wh$x$y"}]), "answers": allow_all},
        {
            "name": "an output named by a location a table holds",
            "event": _run([a], [_located()]),
            "answers": allow_all | {"located": {_LOCATION: _UD}},
        },
        {
            "name": "an output named by a location a table holds, not granted",
            "event": _run([a], [_located()]),
            "answers": {"allowed": {read: [_obj(_UA)]}, "located": {_LOCATION: _UD}, "governed": [_obj(_UD)]},
        },
        {"name": "an output named by a location no table holds", "event": _run([a], [_located()]), "answers": allow_all},
        {
            "name": "an output symlinked to a table the caller sees",
            "event": _run([a], [b | {"facets": b["facets"] | _symlinks((_CATALOG, "wh$gold$pages"))}]),
            "answers": allow_all | {"tables": {"wh$gold$pages": {"table_uuid": _UC, "namespace_uuid": _UNS}}},
        },
        {
            "name": "an output symlinked to a table the caller may not see",
            "event": _run([a], [b | {"facets": b["facets"] | _symlinks((_CATALOG, "wh$secret$t"))}]),
            "answers": allow_all,
        },
        {
            "name": "an output symlinked to a location a table holds",
            "event": _run([a], [b | {"facets": b["facets"] | _symlinks(("s3://lake", "wh/raw/pages.lance"))}]),
            "answers": {"allowed": {write: [_obj(_UB)], read: [_obj(_UA)]}, "located": {_LOCATION: _UD}, "governed": [_obj(_UD)]},
        },
        {"name": "an input not granted", "event": _run([a, c], [b]), "answers": {"allowed": {write: [_obj(_UB)], read: [_obj(_UA)]}}},
        {"name": "an input with no uuid", "event": _run([a, _table("wh$raw$scans")], [b]), "answers": allow_all},
        {
            "name": "inputs outside the catalog are exempt",
            "event": _run(
                [
                    _located("s3://bucket", "raw/pages"),
                    {"namespace": "file", "name": "/data/pages"},
                    {"namespace": "lance", "name": "plain"},
                    {"namespace": _OTHER_CATALOG, "name": "wh$raw$pages"},
                ],
                [b],
            ),
            "answers": allow_all,
        },
        {
            "name": "a bare file namespace naming a catalog table is not exempt",
            "event": _run([{"namespace": "file", "name": "wh$raw$pages"}], [b]),
            "answers": allow_all,
        },
        {
            "name": "an input named by a location a table holds, not granted",
            "event": _run([_located()], [b]),
            "answers": {"allowed": {write: [_obj(_UB)]}, "located": {_LOCATION: _UD}},
        },
        {
            "name": "an input named by a location a table holds, granted",
            "event": _run([_located()], [b]),
            "answers": allow_all | {"located": {_LOCATION: _UD}},
        },
        {
            "name": "an input symlinked to a table the caller may not see",
            "event": _run([a | {"facets": a["facets"] | _symlinks((_CATALOG, "wh$secret$t"))}], [b]),
            "answers": allow_all,
        },
        {
            "name": "a column upstream that is an input",
            "event": _run([a], [b | {"facets": b["facets"] | _columns((_CATALOG, "wh$raw$pages"))}]),
            "answers": allow_all,
        },
        {
            "name": "a column upstream no dataset of the event names",
            "event": _run([a], [b | {"facets": b["facets"] | _columns((_CATALOG, "wh$raw$other"))}]),
            "answers": allow_all,
        },
        {
            "name": "a column upstream named by a location a table holds",
            "event": _run([a], [b | {"facets": b["facets"] | _columns(("s3://lake", "wh/raw/pages.lance"))}]),
            "answers": {"allowed": {write: [_obj(_UB)], read: [_obj(_UA)]}, "located": {_LOCATION: _UD}},
        },
        {
            "name": "a column upstream outside the catalog",
            "event": _run([a], [b | {"facets": b["facets"] | _columns(("s3://bucket", "raw/x"))}]),
            "answers": allow_all,
        },
        {
            "name": "maintenance: the maintainer's rung suffices",
            "relations": maintenance,
            "event": _run([], [b]),
            "answers": {"allowed": {maintain: [_obj(_UB)]}},
        },
        {
            "name": "maintenance: the writer's rung suffices",
            "relations": maintenance,
            "event": _run([], [b]),
            "answers": {"allowed": {write: [_obj(_UB)]}},
        },
        {
            "name": "maintenance: neither rung",
            "relations": maintenance,
            "event": _run([], [b, c]),
            "answers": {"allowed": {maintain: [_obj(_UC)]}, "governed": [_obj(_UB)]},
        },
        {
            "name": "attested: nothing re-derived",
            "attested": True,
            "event": _run([a], [b]),
            "answers": {},
        },
        {
            "name": "attested: an output with no uuid is still refused",
            "attested": True,
            "event": _run([a], [_table("wh$clean$scans")]),
            "answers": {},
        },
        {
            "name": "attested: an input with no uuid is still refused",
            "attested": True,
            "event": _run([_table("wh$raw$scans")], [b]),
            "answers": {},
        },
        {
            "name": "attested: a location output names no table",
            "attested": True,
            "event": _run([a], [_located()]),
            "answers": {"located": {_LOCATION: _UD}},
        },
        {"name": "the decider is down for a check", "event": _run([a], [b]), "answers": {"unavailable": ["batch_check"]}},
        {"name": "the decider is down for a location", "event": _run([a], [_located()]), "answers": {"unavailable": ["locate"]}},
        {
            "name": "the decider is down for a name",
            "event": _run([a], [b | {"facets": b["facets"] | _symlinks((_CATALOG, "wh$gold$pages"))}]),
            "answers": {"unavailable": ["check_tables"]},
        },
        {"name": "a dataset event, granted", "event": _dataset_event("create_table"), "answers": allow_all},
        {"name": "a dataset event, not granted", "event": _dataset_event("drop_columns"), "answers": {"governed": [_obj(_UA)]}},
        {"name": "a dataset event with no uuid", "event": _dataset_event("create_table", uuid=None), "answers": allow_all},
        {
            "name": "names outside ascii are sorted by code point",
            "event": _run([], [_table("wh$ö$x"), _table("wh$z$x"), _table("wh$Å$x"), _table("wh$a$x")]),
            "answers": allow_all,
        },
    ]
    for case in cases:
        case.setdefault("subject", _ADA)
    return cases


def _bus_cases() -> list[dict[str, Any]]:
    """The bus and outbox routes' question: whose authority an event is judged under, and what that authority may record."""
    a, b = _table("wh$raw$pages", _UA), _table("wh$clean$pages", _UB)
    write, read, maintain = "can_write_data", "can_get_metadata", "can_maintain"
    granted = {"allowed": {write: [_obj(_UA), _obj(_UB)], read: [_obj(_UA), _obj(_UB)]}}
    signed = {"signers": [_CATALOG_ID, "flows"], "delegators": [_CATALOG_ID]}
    by_catalog = signed | {"signature": {"verified": _CATALOG_ID, "on_behalf_of": _ADA}}
    by_flows = signed | {"signature": {"verified": "flows"}}
    cases: list[dict[str, Any]] = [
        {"name": "unsigned where signing is enforced", "signature": "refused", **signed, "event": _authored(_run([a], [b]), _ADA)},
        {"name": "the signer's keys could not be read", "signature": "unavailable", **signed, "event": _authored(_run([a], [b]), _ADA)},
        {"name": "a drop the catalog attests", **by_catalog, "event": _authored(_dataset_event("drop_table"), _ADA)},
        {"name": "a deregister the catalog attests", **by_catalog, "event": _authored(_dataset_event("DEREGISTER_TABLE"), _ADA)},
        {"name": "a drop the catalog attests, authorization off", **by_catalog, "fga": False, "event": _dataset_event("drop_table")},
        {"name": "a drop another signer signed", **by_flows, "event": _authored(_dataset_event("drop_table"), _ADA), "answers": {"governed": [_obj(_UA)]}},
        {"name": "a drop nobody signed, grants revoked", "event": _authored(_dataset_event("drop_table"), _ADA), "answers": {"governed": [_obj(_UA)]}},
        {"name": "authorization off", "fga": False, "event": _run([a], [b])},
        {"name": "no author", "event": _run([a], [b]), "answers": granted},
        {"name": "no author, a replay", "replay": True, "event": _run([a], [b]), "answers": granted},
        {"name": "a blank author", "event": _authored(_run([a], [b]), "  "), "answers": granted},
        {"name": "an author that is no principal", "event": _authored(_run([a], [b]), "ada"), "answers": granted},
        {"name": "an author that is no principal, quoted", "event": _authored(_run([a], [b]), 'it\'s "ada"\t\x01'), "answers": granted},
        {"name": "an author that is no principal, long", "event": _authored(_run([a], [b]), "x" * 200), "answers": granted},
        {"name": "an author granted", "event": _authored(_run([a], [b]), _ADA), "answers": granted},
        {"name": "an author not granted", "event": _authored(_run([a], [b]), _ADA), "answers": {"governed": [_obj(_UB)]}},
        {"name": "an author not granted, a replay", "replay": True, "event": _authored(_run([a], [b]), _ADA), "answers": {"governed": [_obj(_UB)]}},
        {"name": "another subject's run", "run_author": _BOB, "event": _authored(_run([a], [b]), _ADA), "answers": granted},
        {"name": "another subject's run, a replay", "replay": True, "run_author": _BOB, "event": _authored(_run([a], [b]), _ADA), "answers": granted},
        {"name": "an output outside the catalog, a replay", "replay": True, "event": _authored(_run([a], [_located("s3://b", "x")]), _ADA)},
        {"name": "a compaction by the maintainer", "event": _authored(_operated("compact_table"), _ADA), "answers": {"allowed": {maintain: [_obj(_UA)]}}},
        {"name": "an expiry by the maintainer", "event": _authored(_operated("expire_versions"), _ADA), "answers": {"allowed": {maintain: [_obj(_UA)]}}},
        {"name": "an index by the writer", "event": _authored(_operated("create_index"), _ADA), "answers": {"allowed": {write: [_obj(_UA)]}}},
        {
            "name": "an append by the maintainer",
            "event": _authored(_operated("append"), _ADA),
            "answers": {"allowed": {maintain: [_obj(_UA)]}, "governed": [_obj(_UA)]},
        },
        {"name": "a write the catalog attests", **by_catalog, "event": _authored(_run([a], [b]), _ADA)},
        {"name": "a write the catalog attests, an output with no uuid", **by_catalog, "event": _authored(_run([a], [_table("wh$x$y")]), _ADA)},
        {"name": "a write another signer signed", **by_flows, "event": _authored(_run([a], [b]), "flows~x"), "answers": granted},
        {"name": "a write another signer signed, not granted", **by_flows, "event": _authored(_run([a], [b]), _ADA), "answers": {"governed": [_obj(_UB)]}},
        {"name": "the decider is down", "event": _authored(_run([a], [b]), _ADA), "answers": {"unavailable": ["batch_check"]}},
        {"name": "the decider is down, a replay", "replay": True, "event": _authored(_run([a], [b]), _ADA), "answers": {"unavailable": ["batch_check"]}},
        {"name": "no decider", "decider": False, "event": _authored(_run([a], [b]), _ADA)},
    ]
    for case in cases:
        case.setdefault("signature", "unenforced")
    return cases


async def _bus_outcome(case: dict[str, Any]) -> dict[str, Any]:
    from lance_namespace import ServiceUnavailableError

    from lakehouse.lineage.signing import VerifiedSignature
    from lineage.api import fga_deps
    from lineage.models import UnverifiedEventError, parse_event

    asked: list[dict[str, Any]] = []
    event = parse_event(case["event"])
    app = _app(case, asked, recorded=event.model_dump(by_alias=True))
    signature = case["signature"]

    async def verified(*_: object) -> VerifiedSignature | None:
        if signature == "refused":
            raise UnverifiedEventError("no listed signer verifies it", "unsigned")
        if signature == "unavailable":
            raise ServiceUnavailableError("the published keys could not be read")
        if isinstance(signature, dict):
            return VerifiedSignature(identity=signature["verified"], kid="k1", on_behalf_of=signature.get("on_behalf_of"))
        return None

    with patch.object(fga_deps, "verify_arrived_event", verified):
        outcome = await _outcome(fga_deps.enforce_bus_authz(event, app, _settings(case), case["event"]))
    return case | {"asked": asked, "outcome": outcome}


async def _output_outcome(case: dict[str, Any]) -> dict[str, Any]:
    from lakehouse.lineage.signing import VerifiedSignature
    from lineage.api import fga_deps
    from lineage.models import parse_event

    asked: list[dict[str, Any]] = []
    signed_by = VerifiedSignature(identity=_CATALOG_ID, kid="k1") if case.get("attested") else None
    settings = _settings(case | ({"signers": [_CATALOG_ID], "delegators": [_CATALOG_ID]} if case.get("attested") else {}))
    relations = tuple(case.get("relations", ["can_write_data"]))
    call = fga_deps.enforce_output_authz(
        parse_event(case["event"]),
        _app(case, asked),
        settings,
        _caller(case["subject"]),
        relations=relations,
        signed_by=signed_by,
    )
    return case | {"asked": asked, "outcome": await _outcome(call)}


def _gate_cases() -> list[dict[str, Any]]:
    """The read doors' gates: the estate rungs and a table's metadata."""
    feed, parks, replay, meta = "can_read_event_feed", "can_read_parked_events", "can_replay_parked_events", "can_get_metadata"
    cases: list[dict[str, Any]] = []
    for gate, relation in (("event_feed_reader", feed), ("parks_reader", parks), ("parks_replayer", replay)):
        cases += [
            {"gate": gate, "name": "authorization off", "fga": False},
            {"gate": gate, "name": "no decider", "decider": False},
            {"gate": gate, "name": "no caller", "subject": None},
            {"gate": gate, "name": "granted", "answers": {"allowed": {relation: [_ROOT]}}},
            {"gate": gate, "name": "not granted", "answers": {"allowed": {relation: ["estate:other"]}}},
            {"gate": gate, "name": "the decider is down", "answers": {"unavailable": ["check"]}},
        ]
    granted = {"allowed": {meta: [_obj(_UA)]}, "described": {"wh$raw$pages": _UA}}
    cases += [
        {"gate": "metadata", "name": "authorization off", "fga": False, "table": "wh$raw$pages", "answers": granted},
        {"gate": "metadata", "name": "granted", "table": "wh$raw$pages", "answers": granted},
        {"gate": "metadata", "name": "not granted", "table": "wh$raw$pages", "answers": {"described": {"wh$raw$pages": _UA}}},
        {"gate": "metadata", "name": "a name the caller may not see", "table": "wh$secret$t", "answers": granted},
        {"gate": "metadata", "name": "no decider", "decider": False, "table": "wh$raw$pages", "answers": granted},
        {"gate": "metadata", "name": "no caller", "subject": None, "bearer": None, "table": "wh$raw$pages", "answers": granted},
        {"gate": "metadata", "name": "no caller, an unseen name", "subject": None, "bearer": None, "table": "wh$x", "answers": granted},
        {"gate": "metadata", "name": "the decider is down", "table": "wh$raw$pages", "answers": granted | {"unavailable": ["check"]}},
        {"gate": "metadata", "name": "a name the catalog refuses", "table": "wh$$pages", "answers": granted | {"refused": ["wh$$pages"]}},
        {"gate": "metadata", "name": "a long refused name is quoted cut", "table": _LONG_NAME, "answers": granted | {"refused": [_LONG_NAME]}},
        {"gate": "metadata", "name": "authorization off, a refused name", "fga": False, "table": "wh$$pages", "answers": {"refused": ["wh$$pages"]}},
    ]
    for case in cases:
        case.setdefault("subject", _ADA)
        case.setdefault("bearer", "bearer-of-ada")
    return cases


#: A name past the most a refusal quotes, holding a quote, so its quoting picks the other one.
_LONG_NAME = "wh$raw$" + "it's" * 80


class _Names:
    """The catalog's describe doors, answering from the case and recording each name asked about. A name the case lists
    under ``refused`` is answered 400, as the catalog answers a name that is no identifier, through the real translation
    of that answer (`lineage.services.names`)."""

    def __init__(self, answers: Answers, asked: list[dict[str, Any]]) -> None:
        self._answers = answers
        self._asked = asked

    async def table(self, name: str, *, bearer: str | None) -> Any:  # noqa: ANN401 - the real DatasetUuids
        from lance_namespace_urllib3_client.exceptions import BadRequestException

        from lakehouse.lineage_events import DatasetUuids
        from lineage.services import names

        self._asked.append({"ask": "describe", "name": name, "bearer": bearer})
        if name in self._answers.get("refused", []):
            with names._described_as_the_callers(name):
                raise BadRequestException(status=400, reason="Bad Request", body='{"error": "the catalog\'s own words", "trace": "t-1"}')
        uuid = self._answers.get("described", {}).get(name)
        return DatasetUuids(table_uuid=uuid, namespace_uuid=_UNS if uuid else None)


def _names(answers: Answers, asked: list[dict[str, Any]]) -> Any:  # noqa: ANN401 - stands in for CatalogNames
    return _Names(answers, asked)


async def _gate_outcome(case: dict[str, Any]) -> dict[str, Any]:
    from lineage.api import fga_deps

    asked: list[dict[str, Any]] = []
    request: Any = SimpleNamespace(app=_app(case, asked))
    settings, caller = _settings(case), _caller(case["subject"])
    gates: dict[str, Callable[[], Awaitable[None]]] = {
        "event_feed_reader": lambda: fga_deps.require_event_feed_reader(request, settings, caller),
        "parks_reader": lambda: fga_deps.require_parks_reader(request, settings, caller),
        "parks_replayer": lambda: fga_deps.require_parks_replayer(request, settings, caller),
        "metadata": lambda: fga_deps.require_metadata_access(
            case.get("table", ""),
            request,
            settings,
            caller,
            _names(case.get("answers", {}), asked),
            case["bearer"],
        ),
    }
    return case | {"asked": asked, "outcome": await _outcome(gates[case["gate"]]())}


def _filter_cases() -> list[dict[str, Any]]:
    """A reader's pages: each item names what it governs, and the filter keeps what the reader may see."""
    meta = "can_get_metadata"
    ta, tb, tc = _obj(_UA), _obj(_UB), _obj(_UC)
    page = [
        {"objects": [ta], "unresolved": []},
        {"objects": [ta, tb], "unresolved": []},
        {"objects": [tc], "unresolved": []},
        {"objects": [ta], "unresolved": ["wh$x$y"]},
        {"objects": [], "unresolved": []},
        {"objects": [], "unresolved": ["wh$x$y"]},
    ]
    sees_a = {"allowed": {meta: [ta]}}
    return [
        {"name": "authorization off", "fga": False, "pages": [page]},
        {"name": "hidden, visible and unresolved", "pages": [page], "answers": sees_a},
        {"name": "a second page asks only what the first did not", "pages": [page[:2], page], "answers": {"allowed": {meta: [ta, tb]}}},
        {"name": "an empty page asks nothing", "subject": None, "pages": [[]]},
        {"name": "a page naming nothing asks nothing", "pages": [[{"objects": [], "unresolved": ["n"]}]], "answers": sees_a},
        {"name": "no caller", "subject": None, "pages": [page], "answers": sees_a},
        {"name": "no decider", "decider": False, "pages": [page], "answers": sees_a},
        {"name": "the decider is down", "pages": [page], "answers": {"unavailable": ["batch_check"]}},
        {
            "name": "an object that is no object id",
            "pages": [[{"objects": ["table:a b", ta], "unresolved": []}, {"objects": ["t#x"], "unresolved": []}]],
            "answers": sees_a,
        },
    ]


async def _filter_outcome(case: dict[str, Any]) -> dict[str, Any]:
    from lance_namespace import LanceNamespaceError

    from lineage.api import fga_deps

    asked: list[dict[str, Any]] = []
    settings = _settings(case)
    request: Any = SimpleNamespace(app=_app(case, asked))
    datasets = fga_deps.DatasetFilter(request, settings, _caller(case.get("subject", _ADA)))
    pages: list[dict[str, Any]] = []
    for items in case["pages"]:
        refs = [fga_deps.Governed(objects=frozenset(item["objects"]), unresolved=frozenset(item["unresolved"])) for item in items]
        undecided = datasets.undecided(obj for ref in refs for obj in ref.objects)
        indices = list(range(len(refs)))
        try:
            kept = await fga_deps.governed(datasets, settings.fga_enabled, indices, lambda index, refs=refs: refs[index])
        except LanceNamespaceError as exc:
            pages.append({"undecided": undecided, "refused": type(exc).__name__, "message": str(exc)})
            break
        pages.append({"undecided": undecided, "kept": kept})
    return case | {"subject": case.get("subject", _ADA), "asked": asked, "outcome": pages}


def _scope_cases() -> list[dict[str, Any]]:
    """Staged events no door has scoped: what each names for the reader, under every identifier it uses."""
    a, b = _table("wh$raw$pages", _UA), _table("wh$clean$pages", _UB)
    events = [
        _run([a], [b]),
        _run([_located()], [b | {"facets": b["facets"] | _symlinks((_CATALOG, "wh$gold$pages"), (_CATALOG, "wh$secret$t"))}]),
        _run([], [_table("wh$x$y") | {"facets": _columns((_CATALOG, "wh$raw$pages"), ("s3://lake", "wh/raw/pages.lance"), (_CATALOG, "wh$q$r"))}]),
        _dataset_event("create_table"),
        _run([{"namespace": "file", "name": "/d"}], []),
    ]
    answers = {"located": {_LOCATION: _UD}, "tables": {"wh$gold$pages": {"table_uuid": _UC, "namespace_uuid": None}}}
    return [
        {"name": "authorization off", "fga": False, "events": events},
        {"name": "aliases, hidden names and column upstreams", "events": events, "answers": answers},
        {"name": "no caller", "subject": None, "events": events, "answers": answers},
        {"name": "no decider", "decider": False, "events": events},
        {"name": "the decider is down", "events": events, "answers": {"unavailable": ["locate"]}},
        {"name": "nothing to resolve asks nothing", "events": [events[0]]},
    ]


async def _scope_outcome(case: dict[str, Any]) -> dict[str, Any]:
    from lance_namespace import LanceNamespaceError

    from lineage.api import fga_deps
    from lineage.models import parse_event

    asked: list[dict[str, Any]] = []
    subject = case.get("subject", _ADA)
    request: Any = SimpleNamespace(app=_app(case, asked))
    datasets = fga_deps.DatasetFilter(request, _settings(case), _caller(subject))
    try:
        scoped = await datasets.scope_unrecorded([parse_event(event) for event in case["events"]])
    except LanceNamespaceError as exc:
        outcome: Any = {"refused": type(exc).__name__, "message": str(exc)}
    else:
        outcome = {"scoped": [{"objects": sorted(scope.objects), "unresolved": sorted(scope.unresolved)} for scope in scoped]}
    return case | {"subject": subject, "asked": asked, "outcome": outcome}


def _stamp_cases() -> list[dict[str, Any]]:
    """The HTTP door's names resolved to the uuids the catalog answers, as the caller."""
    event = _run(
        [
            _table("wh$raw$pages", "claimed-uuid") | {"facets": {"rask_lance": {"tableUuid": "claimed", "ref": "exp"}, "other": {"a": 1}}},
            _located(),
            _located("s3://bucket", "nowhere/x", rask_lance={"tableUuid": "claimed"}),
            {"namespace": "file", "name": "/d"},
        ],
        [
            _table("wh$clean$pages"),
            _table("wh$secret$t", "claimed"),
            {"namespace": _CATALOG, "name": "wh$clean$pages", "facets": {"rask_lance": {"namespaceUuid": "n", "_producer": "p"}}},
        ],
    )
    tables = {"wh$raw$pages": {"table_uuid": _UA, "namespace_uuid": _UNS}, "wh$clean$pages": {"table_uuid": _UB, "namespace_uuid": None}}
    described = {"wh$raw$pages": _UA, "wh$clean$pages": _UB}
    return [
        {"name": "the catalog decides names", "event": event, "answers": {"tables": tables, "located": {_LOCATION: _UD}}},
        {"name": "the store decides, so the catalog describes each name", "event": event, "answers": {"resolves_names": False, "described": described}},
        {"name": "no decider", "decider": False, "event": event, "answers": {"described": described}},
        {"name": "no caller", "subject": None, "event": event, "answers": {"tables": tables, "described": described}},
        {"name": "a dataset event", "event": _dataset_event("create_table", uuid="claimed"), "answers": {"tables": tables}},
        {"name": "nothing to resolve asks nothing", "event": _run([{"namespace": "file", "name": "/d"}], []), "answers": {}},
        {
            "name": "the store decides, and the catalog refuses a name",
            "event": event,
            "answers": {"resolves_names": False, "described": described, "refused": ["wh$clean$pages"]},
        },
        {
            "name": "no decider, and the catalog refuses a name",
            "decider": False,
            "event": event,
            "answers": {"described": described, "refused": ["wh$raw$pages"]},
        },
    ]


async def _stamp_outcome(case: dict[str, Any]) -> dict[str, Any]:
    from lineage.models import parse_event
    from lineage.services.names import stamp_resolved_uuids

    asked: list[dict[str, Any]] = []
    answers = case.get("answers", {})
    event = parse_event(case["event"])
    decider = _Decider(answers, asked) if case.get("decider", True) else None
    subject = case.get("subject", _ADA)
    from lance_namespace import LanceNamespaceError

    try:
        await stamp_resolved_uuids(event, _names(answers, asked), bearer="bearer-of-ada", decider=decider, subject=subject)
    except LanceNamespaceError as exc:
        return case | {"subject": subject, "asked": asked, "refused": type(exc).__name__, "message": str(exc)}
    return case | {"subject": subject, "asked": asked, "stamped": event.model_dump_json(by_alias=True)}


def _author_cases() -> list[dict[str, Any]]:
    from lineage.api import fga_deps
    from lineage.models import parse_event

    events = [
        _run(),
        _authored(_run(), "dex~mallory"),
        _dataset_event("create_table"),
        _authored(_dataset_event("create_table"), "dex~mallory"),
        {"eventTime": _T, "dataset": {"namespace": _CATALOG, "name": "t"}},
    ]
    cases: list[dict[str, Any]] = []
    for raw in events:
        for subject in (_ADA, None):
            event = parse_event(raw)
            fga_deps.enforce_author(event, _caller(subject))
            cases.append({"event": raw, "subject": subject, "stamped": event.model_dump_json(by_alias=True)})
    return cases


def _ref_cases() -> list[dict[str, Any]]:
    """What a list of datasets governs, as inputs and as outputs."""
    from lineage.api import fga_deps

    lists: list[tuple[list[Any], list[str]]] = [
        ([_table("a$b", _UA), _table("a$c")], []),
        ([_table("a$b", _UA), _located("s3://x", "k", rask_lance={"tableUuid": _UB})], []),
        ([{"namespace": "file", "name": "/d"}, {"namespace": "lance", "name": "a$b", "facets": {"rask_lance": {"tableUuid": _UC}}}], []),
        ([_table("a$b", _UA), {"namespace": _OTHER_CATALOG, "name": "a$b"}], ["a$b", "a$q", "a$q"]),
        ([_table("a$b"), _table("a$b", _UA)], ["a$b"]),
        ([{"namespace": _CATALOG, "name": "", "facets": {}}, _table("a$b", "")], []),
        ([], ["x"]),
    ]
    cases: list[dict[str, Any]] = []
    for datasets, upstreams in lists:
        named = fga_deps.governed_refs(datasets, column_upstreams=upstreams)
        written = fga_deps.output_refs(datasets)
        cases.append(
            {
                "datasets": datasets,
                "column_upstreams": upstreams,
                "governed": {"objects": sorted(named.objects), "unresolved": sorted(named.unresolved)},
                "outputs": {"objects": sorted(written.objects), "unresolved": sorted(written.unresolved)},
            }
        )
    return cases


#: What the catalog's decision door answers in each mode, beside a well-formed 200.
_DOOR_MODES = ("answers", "refuses the service", "fails", "answers malformed", "leaves an object out", "answers one nobody asked")


async def _catalog_decider_cases() -> list[dict[str, Any]]:
    """The catalog decider's requests for each question and what each answer of the door comes to."""
    import httpx
    from lance_namespace import ServiceUnavailableError

    from lakehouse.governed.decider import CatalogDecider

    many = [f"table:o{n:04d}" for n in range(2100)]
    many = many + many[:5]
    questions: list[tuple[str, dict[str, Any]]] = [
        ("batch_check", {"user": _ADA, "relation": "can_get_metadata", "objects": many}),
        ("batch_check", {"user": _ADA, "relation": "can_get_metadata", "objects": ["table:b", "table:a", "table:b"]}),
        ("check", {"user": _ADA, "relation": "can_read_event_feed", "obj": _ROOT}),
        ("check_tables", {"user": _ADA, "relation": "can_get_metadata", "tables": ["wh$a$b", "wh$c$d"]}),
        ("locate", {"locations": [_LOCATION, "s3://x/y"]}),
        ("governed", {"objects": ["table:a", "table:b"]}),
        ("list_users", {"relation": "can_get_metadata", "obj": "table:a"}),
    ]
    cases: list[dict[str, Any]] = []
    for method, arguments in questions:
        for mode in _DOOR_MODES if len(arguments.get("objects", [])) < 100 else ("answers",):
            sent: list[dict[str, Any]] = []

            def answer(request: httpx.Request, mode: str = mode, sent: list[dict[str, Any]] = sent) -> httpx.Response:
                body = json.loads(request.content)
                sent.append({"route": request.url.path, "authorization": request.headers.get("authorization"), "body": body})
                if mode == "refuses the service":
                    return httpx.Response(403, json={"error": "no decision role"})
                if mode == "fails":
                    return httpx.Response(500, text="boom")
                if mode == "answers malformed":
                    return httpx.Response(200, text="{")
                return httpx.Response(200, json=_door_answer(request.url.path, body, mode))

            decider = CatalogDecider("http://catalog/", token=lambda: "service-token", timeout_seconds=5, transport=httpx.MockTransport(answer))
            try:
                result: Any = await getattr(decider, method)(**arguments)
            except ServiceUnavailableError as exc:
                outcome: dict[str, Any] = {"refused": type(exc).__name__, "message": str(exc)}
            else:
                outcome = {"answered": {name: decision.model_dump() for name, decision in result.items()} if method == "check_tables" else result}
            cases.append({"method": method, "arguments": arguments, "mode": mode, "sent": sent, "outcome": outcome})
    return cases


def _door_answer(route: str, body: dict[str, Any], mode: str) -> dict[str, Any]:
    """The door's answer to one question: object ``n`` is allowed where ``n`` is even, a table is known where its name ends in ``b``."""
    asked = list(body.get("objects") or body.get("tables") or body.get("locations") or [])
    if mode == "leaves an object out":
        asked = asked[1:]
    elif mode == "answers one nobody asked":
        asked = [*asked, "table:nobody"]
    if route.endswith("/list-users"):
        return {"users": ["dex~ada", "dex~bob"]}
    if route.endswith("/locate"):
        return {"located": {location: _UD if location == _LOCATION else None for location in asked}}
    if route.endswith("/governed"):
        return {"governed": {obj: obj.endswith("a") for obj in asked}}
    if body.get("tables"):
        known = {"allowed": True, "table_uuid": _UA, "namespace_uuid": _UNS}
        return {"tables": {name: known if name.endswith("b") else {"allowed": False} for name in asked}}
    return {"allowed": {obj: index % 2 == 0 for index, obj in enumerate(asked)}}


def authz_vectors() -> dict[str, list[Any]]:
    """Each gate's cases with the questions the Python asks and what it comes to."""
    from lineage.api import fga_deps
    from lineage.core.config import get_settings
    from lineage.models import MAINTENANCE_OPERATIONS

    async def all_cases() -> dict[str, list[Any]]:
        return {
            "output_authz": [await _output_outcome(case) for case in _output_cases()],
            "bus_authz": [await _bus_outcome(case) for case in _bus_cases()],
            "gates": [await _gate_outcome(case) for case in _gate_cases()],
            "filter": [await _filter_outcome(case) for case in _filter_cases()],
            "scope_unrecorded": [await _scope_outcome(case) for case in _scope_cases()],
            "stamp": [await _stamp_outcome(case) for case in _stamp_cases()],
            "catalog_decider": await _catalog_decider_cases(),
        }

    operations = [*sorted(MAINTENANCE_OPERATIONS), "append", "create_table", "COMPACT_TABLE", "", None]
    return {
        "settings": [{"catalog": get_settings().catalog_dataset_namespace, "root": _ROOT}],
        "relations": [{"operation": operation, "relations": list(fga_deps.relations_for_operation(operation))} for operation in operations],
        "refs": _ref_cases(),
        "author": _author_cases(),
        **asyncio.run(all_cases()),
    }
