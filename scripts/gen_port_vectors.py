#!/usr/bin/env python
"""Generate ``rust/vectors/``: what the Python functions answer for a corpus, for the Rust port to answer the same.

Each vector file holds inputs and the results the Python implementation gives for them (ADR 0217 §5). The Rust tests
read the files and assert their own results are equal, so a port that drifts from the Python it replaces fails there.
``make rust-vectors`` writes them and ``make rust-check`` fails on drift. Run: ``uv run scripts/gen_port_vectors.py``.

A case whose result the port deliberately does not reproduce is not in the corpus: it is named in the ADR of the
feature it belongs to.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request


_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "services"))  # `uv run` doesn't apply the pytest pythonpath
os.environ.setdefault("LANCE_S3_ACCESS_KEY_ID", "spec")
os.environ.setdefault("LANCE_S3_SECRET_ACCESS_KEY", "spec")

_OUT = _ROOT / "rust" / "vectors"
_RECEIVED = datetime(2026, 1, 1, tzinfo=UTC)
_CATALOG = "lance-namespace://localhost/api/catalog"
_T = "2026-11-17T10:00:00Z"
_UUID_A = "0190a1b2-0000-7000-8000-00000000000a"
_UUID_B = "0190a1b2-0000-7000-8000-00000000000b"
_UUID_NS = "0190a1b2-0000-7000-8000-0000000000ff"
#: A case that probes a size bound: only whether it is taken is recorded, so the files stay small.
_BOUND = "bound: "


def _lance(**members: Any) -> dict[str, Any]:  # noqa: ANN401 - a facet is arbitrary JSON
    return {"rask_lance": members}


def _run(event_type: str = "COMPLETE", **members: Any) -> dict[str, Any]:  # noqa: ANN401 - an event is arbitrary JSON
    return {"eventType": event_type, "eventTime": _T, "run": {"runId": "run-1"}, "job": {"namespace": "jobs", "name": "copy"}} | members


def _table(name: str, uuid: str | None = None, **facets: Any) -> dict[str, Any]:  # noqa: ANN401 - a facet is arbitrary JSON
    lance = _lance(tableUuid=uuid) if uuid else {}
    return {"namespace": _CATALOG, "name": name, "facets": lance | facets}


def _events() -> dict[str, tuple[Any, list[str]]]:
    """The corpus: a name for each case, the body posted, and the table uuids its outputs resolved to under another name."""
    source, target = _table("wh$raw$pages", _UUID_A), _table("wh$clean$pages", _UUID_B)
    parent = {"run": {"runId": "parent-1"}, "job": {"namespace": "jobs", "name": "nightly"}}
    upstream = {"namespace": _CATALOG, "name": "wh$raw$pages", "field": "text"}
    lineage = {
        "columnLineage": {"fields": {"text": {"inputFields": [upstream, {"namespace": "s3://bucket", "name": "k", "field": "f"}, {"name": "", "field": "f"}]}}}
    }
    cases: dict[str, tuple[Any, list[str]]] = {
        "run start, nothing else": (_run("START"), []),
        "run complete with an edge": (_run(inputs=[source], outputs=[target]), []),
        "run fail writes no edge": (_run("FAIL", inputs=[source], outputs=[target]), []),
        "run abort": (_run("ABORT"), []),
        "lowercase complete succeeds but is keyed by its time": (_run("complete", inputs=[source], outputs=[target]), []),
        "running": (_run("RUNNING", inputs=[source]), []),
        "tables named with no uuid are unresolved": (_run(inputs=[_table("wh$raw$pages")], outputs=[_table("wh$clean$pages")]), []),
        "a table read into itself adds no edge": (_run(inputs=[source], outputs=[source, target]), []),
        "two inputs and two outputs": (_run(inputs=[source, _table("wh$raw$scans")], outputs=[target, _table("wh$clean$scans")]), []),
        "versions, as a number and as text": (
            _run(inputs=[_table("a$b$c", _UUID_A, version={"datasetVersion": 7})], outputs=[_table("a$b$d", _UUID_B, version={"datasetVersion": " 12 "})]),
            [],
        ),
        "versions that are no whole number": (
            _run(inputs=[_table("a$b$c", _UUID_A, version={"datasetVersion": -1})], outputs=[_table("a$b$d", _UUID_B, version={"datasetVersion": "1.5"})]),
            [],
        ),
        "an output's version in its output facets": (
            _run(inputs=[source], outputs=[{"namespace": _CATALOG, "name": "a$b$d", "outputFacets": {"version": {"datasetVersion": "3"}}}]),
            [],
        ),
        "an input read from a branch": (
            _run(inputs=[_table("a$b$c") | {"facets": _lance(tableUuid=_UUID_A, ref="exp", branchIdentifier="b-1")}], outputs=[target]),
            [],
        ),
        "a run that wrote a branch": (
            _run(run={"runId": "run-2", "facets": _lance(ref="exp", branchIdentifier="b-9", operation="append")}, inputs=[source], outputs=[target]),
            [],
        ),
        "a branch identifier with no branch is not kept": (
            _run(run={"runId": "run-3", "facets": _lance(branchIdentifier="b-9")}, inputs=[source], outputs=[target]),
            [],
        ),
        "a parent run": (_run(run={"runId": "run-4", "facets": {"parent": parent}}, inputs=[source], outputs=[target]), []),
        "a parent run with a root": (_run(run={"runId": "run-4", "facets": {"parent": parent | {"root": parent}}}, inputs=[source], outputs=[target]), []),
        "a parent facet with no job names no writer": (
            _run(run={"runId": "run-5", "facets": {"parent": {"run": {"runId": "p"}}}}, inputs=[source], outputs=[target]),
            [],
        ),
        "author by name": (_run(run={"runId": "run-6", "facets": {"rask_author": {"name": "Ada", "sub": "dex~ada"}}}), []),
        "author by sub": (_run(run={"runId": "run-6", "facets": {"rask_author": {"sub": "dex~ada"}}}), []),
        "author as text": (_run(run={"runId": "run-6", "facets": {"rask_author": "ada"}}), []),
        "author from the job's owners": (
            _run(job={"namespace": "jobs", "name": "copy", "facets": {"ownership": {"owners": [{"type": "x"}, {"name": "team-a"}, {"name": "team-b"}]}}}),
            [],
        ),
        "an empty author facet falls to the owners": (
            _run(
                run={"runId": "run-7", "facets": {"rask_author": {"name": ""}}},
                job={"namespace": "jobs", "name": "copy", "facets": {"ownership": {"owners": [{"name": "team-a"}]}}},
            ),
            [],
        ),
        "a signature on a run is stored as sent": (_run(run={"runId": "run-8", "facets": {"rask_signature": {"sig": "x"}, "other": {"a": 1}}}), []),
        "members lineage does not read are kept": (
            _run(schemaURL="https://openlineage.io/spec/2-0-2/OpenLineage.json", producer="https://example/producer", custom={"a": [1, 2.5, None, True]})
            | {"run": {"runId": "run-9", "extra": 1}, "job": {"namespace": "jobs", "name": "copy", "extra": "x"}},
            [],
        ),
        "snake case members are taken": ({"event_type": "COMPLETE", "event_time": _T, "run": {"run_id": "run-10"}, "job": {"namespace": "j", "name": "n"}}, []),
        "external sources": (
            _run(
                inputs=[
                    {"namespace": "s3://bucket", "name": "raw/pages"},
                    {"namespace": "file", "name": "/data/pages"},
                    {"namespace": "file", "name": "wh$raw$pages"},
                    {"namespace": "lance-namespace://other/api/catalog", "name": "wh$raw$pages"},
                    {"namespace": _CATALOG + "/", "name": "wh$raw$scans"},
                    {"namespace": "lance", "name": "plain"},
                    {"namespace": "other", "name": "plain"},
                ],
                outputs=[target],
            ),
            [],
        ),
        "an external location that is a governed table": (
            _run(inputs=[{"namespace": "s3://bucket", "name": "k", "facets": _lance(tableUuid=_UUID_A)}], outputs=[target]),
            [],
        ),
        "namespace uuids": (
            _run(
                inputs=[_table("a$b$c") | {"facets": _lance(tableUuid=_UUID_A, namespaceUuid=_UUID_NS)}],
                outputs=[_table("a$b$d") | {"facets": _lance(namespaceUuid=_UUID_NS)}],
            ),
            [],
        ),
        "the same table twice is one scope entry": (_run(inputs=[source, source], outputs=[target]), []),
        "aliased tables join the scope, sorted": (_run(inputs=[source], outputs=[target]), ["zz-uuid", _UUID_A, "aa-uuid"]),
        "symlinks and column lineage": (
            _run(
                inputs=[source],
                outputs=[
                    target
                    | {
                        "facets": target["facets"]
                        | lineage
                        | {"symlinks": {"identifiers": [{"namespace": "s3://b", "name": "k", "type": "LOCATION"}, {"name": "x"}]}}
                    }
                ],
            ),
            [],
        ),
        "names outside ascii": (_run(run={"runId": "körning-1"}, inputs=[_table("wh$rå$sidor")], outputs=[_table("wh$ren$sidor \U0001f4c4 \x7f")]), []),
        "a dataset event nobody signed": ({"eventTime": _T, "producer": "p", "dataset": _table("wh$raw$pages", _UUID_A, rask_author={"sub": "dex~ada"})}, []),
        "a dataset event with no author": ({"eventTime": _T, "dataset": _table("wh$raw$pages", _UUID_A)}, []),
        "a signed dataset event": (
            {
                "eventTime": _T,
                "producer": "p",
                "dataset": _table("wh$raw$pages") | {"facets": _lance(tableUuid=_UUID_A, operation="drop_table") | {"rask_signature": {"sig": "x"}}},
            },
            [],
        ),
        "a dataset event's owner": ({"eventTime": _T, "dataset": _table("wh$raw$pages", ownership={"owners": [{"name": "team-a"}, {"name": "team-b"}]})}, []),
        "a dataset event's author by name": ({"eventTime": _T, "dataset": _table("wh$raw$pages", rask_author={"name": "Ada"}), "custom": 1}, []),
        "a dataset event with a job is refused": ({"eventTime": _T, "dataset": _table("t"), "job": {"namespace": "j", "name": "n"}}, []),
        "a dataset beside a null run is read as a run event": ({"eventTime": _T, "dataset": _table("t"), "run": None}, []),
        "no event type": ({"eventTime": _T, "run": {"runId": "r"}, "job": {"namespace": "j", "name": "n"}}, []),
        "no run id": ({"eventType": "START", "eventTime": _T, "run": {}, "job": {"namespace": "j", "name": "n"}}, []),
        "null inputs": (_run(inputs=None), []),
        "null facets": (_run(inputs=[{"namespace": "n", "name": "x", "facets": None}]), []),
        "a namespace that is a number": (_run(inputs=[{"namespace": 5, "name": "x"}]), []),
        "a producer that is a number": (_run(producer=5), []),
        f"{_BOUND}65 inputs": (_run(inputs=[{"namespace": "n", "name": f"t{n}"} for n in range(65)]), []),
        f"{_BOUND}64 inputs": (_run(inputs=[{"namespace": "n", "name": f"t{n}"} for n in range(64)]), []),
        f"{_BOUND}1001 names": (
            _run(outputs=[_table("d", columnLineage={"fields": {"c": {"inputFields": [{"name": f"u{n}", "field": "f"} for n in range(1000)]}}})]),
            [],
        ),
        f"{_BOUND}1000 names": (
            _run(outputs=[_table("d", columnLineage={"fields": {"c": {"inputFields": [{"name": f"u{n}", "field": "f"} for n in range(999)]}}})]),
            [],
        ),
        "a list": ([1, 2], []),
        "text": ("event", []),
        "null": (None, []),
    }
    return cases


def _without(row: dict[str, Any], *dropped: str) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in dropped}


def _event_vectors() -> list[dict[str, Any]]:
    from pydantic import ValidationError

    from lineage.api.v1.endpoints.events import _column_lineage_datasets, targeting_view
    from lineage.models import author_sub_from_payload, parse_event, run_id_from_payload, without_signature_facet
    from lineage.services.provenance import edges_of, feed_row

    vectors: list[dict[str, Any]] = []
    for name, (body, aliased) in _events().items():
        vector: dict[str, Any] = {"name": name, "input": body}
        try:
            event = parse_event(body)
        except (ValidationError, ValueError, TypeError):
            event = None
        vector |= {"ok": event is not None}
        if not name.startswith(_BOUND):
            vector |= {
                "aliased": aliased,
                "author_sub": author_sub_from_payload(body),
                "run_id": run_id_from_payload(body),
                "without_signature": without_signature_facet(body) if isinstance(body, dict) else None,
            }
        if event is None or name.startswith(_BOUND):
            vectors.append(vector)
            continue
        stored = event.model_dump_json(by_alias=True)
        row = feed_row(event, received_at=_RECEIVED, aliased=aliased)
        vectors.append(
            vector
            | {
                "stored": stored,
                "row": _without(row, "received_at"),
                "edges": [_without(edge, "received_at") for edge in edges_of(event, received_at=_RECEIVED)],
                "targeting_view": targeting_view(json.loads(stored)),
                "column_lineage": sorted(_column_lineage_datasets(json.loads(stored))),
            }
        )
    return vectors


def _function_vectors() -> dict[str, Any]:
    from lakehouse.lineage_events import static_event_id
    from lakehouse.naming import is_external_source_namespace
    from lineage.api.v1.endpoints.events import _forward_cursor, _next_chunk
    from lineage.schemas import EventRecord

    def records(seqs: list[int]) -> list[EventRecord]:
        return [EventRecord(seq=seq, event={}) for seq in seqs]

    names = [
        ("s3://b", "k"),
        ("file", "/x"),
        ("file", "a$b"),
        ("lance", "x"),
        ("lance", "a$b"),
        ("other", "x"),
        ("", "x"),
        (_CATALOG, "a$b"),
        (_CATALOG + "//", "a$b"),
    ]
    names += [("lance-namespace://other", "a$b"), ("lance-namespace:/x", "a"), ("lance-namespace", "a")]
    cursors = [
        ([], [], 3, 5, 9),
        ([], [], 3, 12, 9),
        ([], [4, 5], 3, 2, 9),
        ([4, 5, 6], [4, 5, 6, 7], 3, 2, 9),
        ([4, 5], [4, 5, 6, 7], 3, 2, 9),
        ([4], [4], 1, 0, 0),
    ]
    texts = ["plain", 'quote" back\\ slash', "tab\t nl\n cr\r bs\b ff\f", "nul\x00 esc\x1b del\x7f", "å ö 中 \U0001f4c4", ""]
    ids = [("p", "ns", "name", "op", _T), ("", "", "", "", ""), ("å", "ns", "wh$a$b", "drop_table", _T)]
    return {
        "external": [
            {"namespace": ns, "name": name, "catalog": _CATALOG, "external": is_external_source_namespace(ns, name, catalog=_CATALOG)} for ns, name in names
        ],
        "static_event_id": [
            {"parts": list(parts), "id": static_event_id(producer=parts[0], namespace=parts[1], name=parts[2], operation=parts[3], event_time=parts[4])}
            for parts in ids
        ],
        "pyjson": [
            {"value": value, "text": json.dumps(value, separators=(",", ":"))} for value in [*texts, [None, 1, -7, True, "å"], {"k\n": ["a", {"b": None}]}]
        ],
        "next_chunk": [
            {"args": list(args), "chunk": _next_chunk(*args)}
            for args in itertools.product([1, 50, 500], [1, 50, 500], [0, 1, 49], [0, 1, 1500], [1, 2, 3])
            if args[2] < args[0]
        ],
        "forward_cursor": [
            {
                "returned": returned,
                "read": read,
                "limit": limit,
                "since": since,
                "head": head,
                "cursor": _forward_cursor(records(returned), records(read), limit=limit, since=since, head=head),
            }
            for returned, read, limit, since, head in cursors
        ],
    }


#: The trace a recorded request names, so its error body's `instance` is known.
_TRACE = "0af7651916cd43dd8448eb211c80319c"
#: The shell's limits while it is recorded: small, so a case reaches each of them quickly.
_MAX_BODY_BYTES = 1024
_DEADLINE_SECONDS = 0.2


def _shell_requests() -> list[dict[str, Any]]:
    """The requests put to the shell: a name, where the service is in its life, which dependencies answer, and what is sent."""
    up = {"catalog": True, "bus": True}

    def case(name: str, method: str, path: str, *, life: str = "started", answering: dict[str, bool] = up, **sent: Any) -> dict[str, Any]:  # noqa: ANN401
        return {"name": name, "life": life, "answering": answering, "method": method, "path": path, "headers": {}, "body": 0, "declared": True} | sent

    raised = [case(f"a door refuses with {name}", "GET", f"/raise/{name}") for name in _error_names()]
    return [
        case("alive", "GET", "/livez"),
        case("alive while starting", "GET", "/livez", life="starting"),
        case("ready", "GET", "/readyz"),
        case("starting", "GET", "/readyz", life="starting"),
        case("draining", "GET", "/readyz", life="draining"),
        case("degraded without an optional dependency", "GET", "/readyz", answering={"catalog": True, "bus": False}),
        case("unhealthy without a required dependency", "GET", "/readyz", answering={"catalog": False, "bus": True}),
        case("no door at the path", "GET", "/nope"),
        case("no door for the method", "POST", "/livez"),
        case("an error names the trace sent", "GET", "/nope", headers={"traceparent": f"00-{_TRACE}-b7ad6b7169203331-01"}),
        case("a malformed traceparent is not the trace", "GET", "/nope", headers={"traceparent": f"00-{_TRACE}-0000000000000000-01"}),
        case("a body at the cap", "POST", "/echo", body=_MAX_BODY_BYTES),
        case("a body declared over the cap", "POST", "/echo", body=_MAX_BODY_BYTES + 1),
        case("a body declared over the cap, to no door", "POST", "/nope", body=_MAX_BODY_BYTES + 1),
        case("a body that runs over the cap undeclared", "POST", "/echo", body=_MAX_BODY_BYTES + 1, declared=False),
        case("a request past its deadline", "GET", "/slow"),
        case("a door that panics", "GET", "/panic"),
        case("a refusal under a name of its own", "GET", "/raise-named"),
        *raised,
    ]


def _error_names() -> list[str]:
    import lance_namespace
    from lance_namespace import ErrorCode

    by_code = {
        int(error.code): name
        for name, error in vars(lance_namespace).items()
        if isinstance(error, type) and issubclass(error, lance_namespace.LanceNamespaceError) and name != "LanceNamespaceError"
    }
    return [by_code[int(code)] for code in ErrorCode]


async def _shell_vectors() -> list[dict[str, Any]]:
    import asyncio
    import logging
    from collections.abc import AsyncGenerator
    from contextlib import asynccontextmanager

    import httpx
    import lance_namespace

    from lakehouse.health import Dependency, HealthWatch
    from lakehouse.lance_app import build_lance_service_app
    from lakehouse.lifecycle import mark_draining, mark_started
    from lakehouse.schemas.health import Readiness
    from lineage.models import UnauthoredRunError

    class Panic(BaseException):
        """What a native library's panic reaches Python as: a BaseException that is no Exception."""

    doors = APIRouter()

    @doors.get("/raise/{name}")
    async def refuse(name: str) -> None:
        raise getattr(lance_namespace, name)("boom")

    @doors.get("/raise-named")
    async def refuse_named() -> None:
        raise UnauthoredRunError("boom")

    @doors.get("/slow")
    async def slow() -> dict[str, str]:
        await asyncio.sleep(5 * _DEADLINE_SECONDS)
        return {}

    @doors.get("/panic")
    async def panic() -> None:
        raise Panic

    @doors.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield

    async def ready(request: Request) -> Readiness:
        return request.app.state.health.readiness()

    app = build_lance_service_app(
        title="shell",
        docs_enabled=False,
        audit_enabled=False,
        lifespan=lifespan,
        log=logging.getLogger("shell"),
        ready_check=ready,
        max_body_bytes=_MAX_BODY_BYTES,
        request_deadline_seconds=_DEADLINE_SECONDS,
        routers=[doors],
    )
    answering: dict[str, bool] = {}

    async def read(name: str) -> None:
        if not answering[name]:
            raise ConnectionError(f"{name} is down")

    async def unsent(size: int) -> AsyncGenerator[bytes]:
        yield b"x" * size

    vectors: list[dict[str, Any]] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shell") as client:
        for case in _shell_requests():
            app.state.startup_complete = app.state.shutting_down = False
            if case["life"] == "started":
                mark_started(app)
            elif case["life"] == "draining":
                mark_draining(app)
            answering.clear()
            answering.update(case["answering"])
            dependencies = [
                Dependency(name="catalog", read=lambda: read("catalog"), required=True),
                Dependency(name="bus", read=lambda: read("bus"), required=False),
            ]
            app.state.health = HealthWatch(dependencies)
            for dependency in dependencies:
                await app.state.health.check(dependency)
            content = (b"x" * case["body"] if case["declared"] else unsent(case["body"])) if case["body"] else None
            answer = await client.request(case["method"], case["path"], headers=case["headers"], content=content)
            body = answer.json() if answer.content else None
            named = body.get("instance") if isinstance(body, dict) else None
            vectors.append(
                case
                | {
                    "status": answer.status_code,
                    "answered": {name: answer.headers[name] for name in ("content-type", "cache-control", "vary", "allow") if name in answer.headers},
                    # The trace an error names: the one sent where one was, else only whether it names one (a new trace is random).
                    "instance": named if named == _TRACE else named is not None,
                    "answer": {key: value for key, value in body.items() if key != "instance"} if isinstance(body, dict) else body,
                }
            )
        await asyncio.sleep(5 * _DEADLINE_SECONDS)
    return vectors


def main() -> None:
    import asyncio

    shell = {"limits": [{"max_body_bytes": _MAX_BODY_BYTES, "deadline_seconds": _DEADLINE_SECONDS}], "exchanges": asyncio.run(_shell_vectors())}
    _OUT.mkdir(parents=True, exist_ok=True)
    for name, vectors in (
        ("lineage_events", {"catalog": [_CATALOG], "cases": _event_vectors()}),
        ("lineage_functions", _function_vectors()),
        ("shell_http", shell),
    ):
        (_OUT / f"{name}.json").write_text(_one_entry_a_line(vectors))
        print(f"wrote rust/vectors/{name}.json")


def _one_entry_a_line(groups: dict[str, list[Any]]) -> str:
    """``groups`` as JSON with each entry of each list on a line of its own, so a changed case is a changed line.

    An entry keeps its members in the order written: an event is stored in the order it was sent, which a vector checks.
    """
    lists = [f"{json.dumps(key)}: [\n{',\n'.join(json.dumps(entry, ensure_ascii=False) for entry in entries)}\n]" for key, entries in sorted(groups.items())]
    return "{\n" + ",\n".join(lists) + "\n}\n"


if __name__ == "__main__":
    main()
