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


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    for name, vectors in (("lineage_events", {"catalog": [_CATALOG], "cases": _event_vectors()}), ("lineage_functions", _function_vectors())):
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
