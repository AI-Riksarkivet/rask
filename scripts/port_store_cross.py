#!/usr/bin/env python
"""Show that the Python and the Rust provenance stores write and read the same tables alike (ADR 0228).

Against a running catalog (``scripts/port_catalog_fixture.py``), in three namespaces of its own:

- ``python``: the Python store records the corpus, then the Rust store records it again;
- ``rust``: the Rust store records the corpus, then the Python store records it again;
- ``mixed``: the Python store records the first half, the Rust store all of it, the Python store all of it again.

It then reads every row of the feed, the edges and the run bindings of each namespace (row ids included, the stamp of
receipt left out, since each store stamps its own clock) and requires the three to be equal, row for row in row-id
order, column for column, with equal schemas (field metadata included; the schema's own metadata names the table it is
read from, so it is left out); requires each store's outcomes to be the same; and puts the same reads to
both stores over the mixed tables (the feed with and without its payload, every page forward and back, the bounds,
the walk, run authors, recorded keys, parked deliveries, the cursor and the feed's uuid) and requires equal answers.
Run, with the fixture serving on port 8765::

    uv run scripts/port_store_cross.py --catalog-url http://127.0.0.1:8765

Exits non-zero, naming what differs, when anything does.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[1]
_PARENT = ["lance_catalog", "port"]
_T = "2026-11-17T10:00:00Z"
_A, _B, _C = ("0190a1b2-0000-7000-8000-00000000000a", "0190a1b2-0000-7000-8000-00000000000b", "0190a1b2-0000-7000-8000-00000000000c")
#: The tables both stores write; the refusals and cursors are written by the Python store only.
_COMPARED = ("events", "edges", "runs")
#: The most pages a paging read takes, well above what the corpus fills.
_PAGES = 20


def _dataset(catalog: str, name: str, table: str | None = None, **lance: Any) -> dict[str, Any]:  # noqa: ANN401 - a facet is arbitrary JSON
    facet = ({"tableUuid": table} if table else {}) | lance
    return {"namespace": catalog, "name": name, "facets": {"rask_lance": facet} if facet else {}}


def _run(run_id: str, event_type: str, author: str | None, *, time: str = _T, **members: Any) -> dict[str, Any]:  # noqa: ANN401
    facets = {"rask_author": {"sub": author}} if author else {}
    return {"eventType": event_type, "eventTime": time, "run": {"runId": run_id, "facets": facets}, "job": {"namespace": "jobs", "name": "copy"}} | members


def _corpus(catalog: str) -> list[dict[str, Any]]:
    a, b, c = _dataset(catalog, "wh$raw$a", _A), _dataset(catalog, "wh$clean$b", _B), _dataset(catalog, "wh$gold$c", _C)
    signed = {"eventTime": _T, "producer": "p", "dataset": _dataset(catalog, "wh$raw$a", _A, operation="create_table")}
    signed["dataset"]["facets"]["rask_signature"] = {"sig": "x"}
    posted = {"eventTime": _T, "producer": "p", "dataset": {"namespace": catalog, "name": "wh$raw$a", "facets": {"rask_author": {"sub": "ada"}}}}
    events = [
        (_run("r1", "START", "ada"), []),
        (_run("r1", "RUNNING", "ada", time="2026-11-17T10:00:01Z"), []),
        (_run("r1", "COMPLETE", "ada", inputs=[a], outputs=[b]), []),
        (_run("r2", "COMPLETE", "bob", inputs=[b, _dataset("s3://bucket", "raw/k")], outputs=[c]), [_A]),
        (signed, []),
        (posted, [_B, _C]),
        (_run("r1", "COMPLETE", "cyd", inputs=[a], outputs=[c]), []),
        (_run("r3", "FAIL", None, inputs=[a], outputs=[c]), []),
        (_run("r4", "COMPLETE", "ada", inputs=[_dataset(catalog, "wh$raw$named")], outputs=[_dataset(catalog, "wh$x$y", ref="exp")]), []),
        (_run("r5", "complete", "ada", inputs=[c], outputs=[a]), []),
    ]
    return [{"data": data, "aliased": aliased} for data, aliased in events]


def _namespace(url: str) -> list[str]:
    import httpx

    name = f"x{uuid.uuid4().hex}"
    created = httpx.post(f"{url}/v1/namespace/{'%24'.join([*_PARENT, name])}/create", params={"delimiter": "$"}, json={}, timeout=30)
    created.raise_for_status()
    return [*_PARENT, name]


def _python_store(url: str, namespace: list[str]) -> Any:  # noqa: ANN401 - the store
    from lineage.services.provenance import ProvenanceStore

    return ProvenanceStore(catalog_url=url, namespace=namespace, token=lambda: None, refuse_foreign_runs=True)


async def _python_record(url: str, namespace: list[str], events: list[dict[str, Any]]) -> list[str]:
    from lineage.models import ForeignRunError, parse_event

    store = _python_store(url, namespace)
    await store.ensure()
    outcomes: list[str] = []
    for event in events:
        try:
            await store.record(parse_event(event["data"]), aliased=event["aliased"])
            outcomes.append("landed")
        except ForeignRunError:
            outcomes.append("foreign_run")
    return outcomes


def _rust(command: str, url: str, namespace: list[str], catalog: str, payload: Any) -> Any:  # noqa: ANN401 - JSON
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as file:
        json.dump(payload, file)
    cargo = ["cargo", "run", "--quiet", "--locked", "-p", "lineage", "--example", "store_cross", "--", command, url, "$".join(namespace), catalog, file.name]
    answered = subprocess.run(cargo, cwd=_ROOT / "rust", check=True, capture_output=True, text=True).stdout  # noqa: S603 - a fixed argv, no shell
    Path(file.name).unlink()
    return json.loads(answered)


def _raw(url: str, namespace: list[str], table: str) -> tuple[str, list[dict[str, Any]]]:
    from lakehouse.lancekit.reader import CatalogTableReader, RestCatalogTransport

    read = CatalogTableReader(RestCatalogTransport(url, timeout=60), [*namespace, table]).to_table(with_row_id=True)
    rows = sorted(read.drop_columns(["received_at"]).to_pylist(), key=lambda row: row["_rowid"])
    return read.schema.to_string(show_schema_metadata=False, show_field_metadata=True), rows


async def _python_reads(url: str, namespace: list[str], queries: dict[str, list[str]]) -> dict[str, Any]:
    store = _python_store(url, namespace)

    async def pages(limit: int, *, forward: bool) -> list[list[int]]:
        found: list[list[int]] = []
        cursor: int | None = 0 if forward else None
        # As in the Rust half, a bound keeps a cursor that does not move from paging for ever.
        while len(found) < _PAGES:
            read = await store.list_events(limit, since=cursor, summary=True) if forward else await store.list_events(limit, after=cursor, summary=True)
            if not read:
                break
            found.append([record.seq for record in read])
            cursor = read[-1].seq
        return found

    derived, complete = await store.derived(set(queries["sources"]))
    bounds = await store.feed_bounds()
    return {
        "events": [record.model_dump() for record in await store.list_events(500, since=0)],
        "newest": [record.model_dump() for record in await store.list_events(3)],
        "forward_pages": await pages(2, forward=True),
        "backward_pages": await pages(3, forward=False),
        "bounds": list(bounds) if bounds else None,
        "derived": [table.model_dump() for table in derived],
        "complete": complete,
        "run_authors": {run: await store.run_author(run) for run in queries["runs"]},
        "recorded": sorted(await store.events_recorded(queries["keys"])),
        "parked": [[parked.key, parked.run_id, parked.feed_key] for parked in await store.parked()],
        "cursor": await store.cursor("forwarder"),
        "incarnation": await store.feed_incarnation(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog-url", required=True)
    url = parser.parse_args().catalog_url.rstrip("/")
    sys.path.insert(0, str(_ROOT / "services"))
    from lineage.core.config import get_settings
    from lineage.services.provenance import feed_key

    catalog = get_settings().catalog_dataset_namespace
    events = _corpus(catalog)
    half = len(events) // 2
    spaces = {name: _namespace(url) for name in ("python", "rust", "mixed")}
    outcomes = {
        "python": asyncio.run(_python_record(url, spaces["python"], events)),
        "rust": _rust("record", url, spaces["rust"], catalog, events),
        "rust again over python": _rust("record", url, spaces["python"], catalog, events),
        "python again over rust": asyncio.run(_python_record(url, spaces["rust"], events)),
        "mixed, python first half": asyncio.run(_python_record(url, spaces["mixed"], events[:half])),
        "mixed, rust": _rust("record", url, spaces["mixed"], catalog, events),
        "mixed, python": asyncio.run(_python_record(url, spaces["mixed"], events)),
    }
    problems: list[str] = []
    if len({json.dumps(outcome) for name, outcome in outcomes.items() if not name.endswith("first half")}) != 1:
        problems.append(f"the stores answered different outcomes: {json.dumps(outcomes, indent=1)}")
    print(f"outcomes: {outcomes['python']}")

    for table in _COMPARED:
        read = {name: _raw(url, namespace, table) for name, namespace in spaces.items()}
        problems += [
            f"{table}: the {name} namespace differs from the python one:\n{read[name]}\n{read['python']}"
            for name in ("rust", "mixed")
            if read[name] != read["python"]
        ]
        print(f"{table}: {len(read['python'][1])} rows, equal in all three namespaces: {all(read[name] == read['python'] for name in spaces)}")

    store = _python_store(url, spaces["mixed"])
    parked = {"id": "d1", "data": _run("r9", "COMPLETE", "ada")}
    asyncio.run(store.record_refusal(outbox_key="dead_letter:d1", run_id="r9", author="ada", reason="denied", event_json=json.dumps(parked)))
    asyncio.run(store.advance_cursor("forwarder", 4))
    from lineage.models import parse_event

    keys = [feed_key(parse_event(event["data"])) for event in events] + ["r9@COMPLETE"]
    queries = {"sources": [_A], "runs": ["r1", "r2", "r3", "r9"], "keys": keys}
    ours, theirs = asyncio.run(_python_reads(url, spaces["mixed"], queries)), _rust("read", url, spaces["mixed"], catalog, queries)
    problems += [
        f"read {key} differs:\n  python: {json.dumps(ours[key])[:2000]}\n  rust:   {json.dumps(theirs.get(key))[:2000]}"
        for key in ours
        if ours[key] != theirs.get(key)
    ]
    print(f"reads compared over the mixed tables: {sorted(ours)}; {len(ours['events'])} feed rows")
    if problems:
        raise SystemExit("\n".join(problems))
    print("the Python and Rust stores wrote identical rows and answered identical reads")


if __name__ == "__main__":
    main()
