#!/usr/bin/env python
"""The Python half of ``scripts/port_doors_check.sh``: lineage's doors put to the Python app and the Rust router at once (ADR 0236).

Three commands, run by the shell script around the services it starts:

    uv run --no-sync scripts/port_doors_fixture.py issuer <port>
    uv run --no-sync scripts/port_doors_fixture.py setup <work> <catalog-url> <openfga-url> <openfga-key>
    uv run --no-sync scripts/port_doors_fixture.py corpus <work> <python-url> <rust-url> <issuer-url> <store-endpoint> <bucket> <store-issuer>

``issuer`` is the identity provider both services trust: discovery, a key set, and at ``/token?sub=<subject>`` a token
signed with an RSA key made when it starts, for lineage's audience. ``setup`` creates the governed tables in the catalog
(their uuids are the catalog's), an OpenFGA store with the estate's model and the grants below, and writes what it made
to ``<work>/setup.json``. ``corpus`` stages outbox events as the catalog's identity and parks deliveries in the
refusals table, then puts every request of the corpus to both services in turn, compares the status, the headers a
client reads and the body member for member, prints every difference and exits non-zero when any is not one ADR 0236
keeps. A write is put to one service and then to the other, each going first in turn, so each reads what the other
wrote; a write that consumes what it acts on (a replay) is put to each with its own object and compared with the run id
read as the same.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any


#: What a request carries: JSON, raw bytes, or nothing.
type Body = dict[str, Any] | list[Any] | bytes | None


ROOT = "estate:rask"
AUDIENCE = "rask-lineage"
CATALOG = "lance-namespace://localhost/api/catalog"
DATA = "lance_catalog$port$data"
PROVENANCE = "lance_catalog$port$doors"
#: The governed tables, by the last segment of their name.
TABLES = ("a", "b", "c")
#: Who holds what: `(subject, relation, object)`, the object a table's last segment or the estate's root.
GRANTS = [
    ("admin", "owner", ROOT),
    ("feed", "event_reader", ROOT),
    *[("admin", "writer", table) for table in TABLES],
    ("ada", "reader", "a"),
    ("ada", "writer", "b"),
    # Lineage's own rungs at the catalog's decision door, where the catalog decides (`RASK_FGA_DECIDER_URL`).
    ("lineage", "decider", ROOT),
    ("lineage", "governance_prober", ROOT),
]
PRODUCER = "system:serviceaccount:rask:rask-sa-catalog"
OUTBOX = "_lineage_outbox"
#: What a client reads of an answer's headers.
HEADERS = ("content-type", "cache-control", "vary", "allow", "location")
#: What the Rust doors answer on purpose where the Python app answers otherwise, by case: ADR 0236 says why.
KEPT = {
    "HEAD on a GET door": "a GET door answers HEAD (ADR 0219)",
    "ingest: a body no event model takes": "the errors list of an event the model refuses is the port's own (ADR 0218)",
    "ingest: a run event with no job": "the errors list of an event the model refuses is the port's own (ADR 0218)",
    "ingest: nested deeper than the reader reads": "a body nested deeper than 512 levels is refused before it is read (ADR 0223)",
    "ingest: nested far deeper": "a body nested deeper than 512 levels is refused before it is read (ADR 0223)",
}


def table(name: str) -> str:
    return f"{DATA}${name}"


def issuer(port: int) -> None:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()

    def b64(n: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()

    public = f"http://127.0.0.1:{port}"
    documents = {
        "/.well-known/openid-configuration": {"issuer": public, "jwks_uri": f"{public}/keys", "id_token_signing_alg_values_supported": ["RS256"]},
        "/keys": {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "doors", "n": b64(numbers.n), "e": b64(numbers.e)}]},
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # the stdlib's name
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/token":
                asked = urllib.parse.parse_qs(url.query)
                now = int(time.time())
                claims = {"iss": public, "sub": asked["sub"][0], "aud": AUDIENCE, "exp": now + 600, "iat": now - 5}
                payload, kind = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "doors"}).encode(), "text/plain"
            elif url.path in documents:
                payload, kind = json.dumps(documents[url.path]).encode(), "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401 - the stdlib's signature
            pass

    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def setup(work: Path, catalog_url: str, fga_url: str, fga_key: str) -> None:
    import httpx
    import pyarrow as pa
    from pyarrow import ipc

    root = Path(__file__).resolve().parents[1]
    catalog = httpx.Client(base_url=catalog_url, timeout=60)
    for namespace in (PROVENANCE, DATA):
        created = catalog.post(f"/v1/namespace/{namespace}/create", json={})
        assert created.status_code in (200, 409), created.text
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, pa.schema([("id", pa.int64())])) as writer:
        writer.write_table(pa.table({"id": pa.array([1], pa.int64())}))
    uuids = {}
    for name in TABLES:
        created = catalog.post(
            f"/v1/table/{table(name)}/create", content=sink.getvalue().to_pybytes(), headers={"content-type": "application/vnd.apache.arrow.stream"}
        )
        assert created.status_code in (200, 409), created.text
        described = catalog.post(f"/v1/table/{table(name)}/describe", json={}).json()
        uuids[name] = described["properties"]["rask.uuid"]
    fga = httpx.Client(base_url=fga_url, headers={"authorization": f"Bearer {fga_key}"}, timeout=30)
    store = fga.post("/stores", json={"name": "doors"}).json()["id"]
    model_text = (root / "packages/lakehouse/src/lakehouse/governed/auth/model.json").read_text()
    model = fga.post(f"/stores/{store}/authorization-models", content=model_text).json()["authorization_model_id"]
    tuples = [{"user": f"user:idp~{who}", "relation": relation, "object": obj if obj == ROOT else f"table:{uuids[obj]}"} for who, relation, obj in GRANTS]
    written = fga.post(f"/stores/{store}/write", json={"writes": {"tuple_keys": tuples}, "authorization_model_id": model})
    assert written.status_code == 200, written.text
    (work / "setup.json").write_text(json.dumps({"store": store, "model": model, "uuids": uuids}))
    print(f"RASK_FGA_STORE_ID={store} RASK_FGA_MODEL_ID={model}")


#: The uuid of each governed table, which the catalog stamps on every event it stages: set by the corpus from `setup`.
UUIDS: dict[str, str] = {}


def _dataset(name: str, stamped: bool = False, **facets: Any) -> dict[str, Any]:  # noqa: ANN401 - a facet is arbitrary JSON
    """A dataset of the catalog by a table's last segment, or one outside it by its URI; ``stamped`` with its uuid."""
    if "://" in name:
        scheme, _, path = name.partition("://")
        host, _, rest = path.partition("/")
        return {"namespace": f"{scheme}://{host}", "name": rest, "facets": facets}
    lance = {"rask_lance": {"tableUuid": UUIDS[name]}} if stamped and name in UUIDS else {}
    return {"namespace": CATALOG, "name": table(name), "facets": lance | facets}


def _run(
    run_id: str,
    outputs: list[str],
    inputs: Sequence[str] = (),
    state: str = "COMPLETE",
    author: str | None = None,
    stamped: bool = False,
    **outputs_facets: Any,  # noqa: ANN401 - a facet is arbitrary JSON
) -> dict[str, Any]:
    run_facets = {"rask_author": {"sub": f"idp~{author}"}} if author else {}
    return {
        "eventType": state,
        "eventTime": "2026-11-17T10:00:00Z" if state == "COMPLETE" else "2026-11-17T09:00:00Z",
        "run": {"runId": run_id, "facets": run_facets},
        "job": {"namespace": "jobs", "name": f"job-{run_id}"},
        "inputs": [_dataset(name, stamped) for name in inputs],
        "outputs": [_dataset(name, stamped, **outputs_facets) for name in outputs],
        "producer": "https://example.org/producer",
    }


def _static(name: str, operation: str = "alter_table") -> dict[str, Any]:
    return {"eventTime": "2026-11-17T11:00:00Z", "producer": "https://example.org/producer", "dataset": _dataset(name, rask_lance={"operation": operation})}


def _envelope(event: dict[str, Any]) -> Any:  # noqa: ANN401 - the envelope
    from lakehouse.cloud_events import lineage_envelope

    return lineage_envelope(event)


def _seed(endpoint: str, bucket: str, store_issuer: str, work: Path) -> dict[str, str]:
    """Stage outbox events as the catalog's identity and park deliveries in the refusals table; the run ids each made."""
    import asyncio

    from lakehouse import outbox
    from lakehouse.storage_options import StorageOptions
    from lakehouse.store_session import WebIdentity, exchange
    from lineage.core.config import LineageSettings
    from lineage.services.provenance import ProvenanceStore

    UUIDS.update(json.loads((work / "setup.json").read_text())["uuids"])
    tag = uuid.uuid4().hex[:6]
    token_file = work / "producer-token"
    with urllib.request.urlopen(f"{store_issuer}/token?{urllib.parse.urlencode({'sub': PRODUCER})}", timeout=10) as answer:  # noqa: S310 - the fixture's issuer
        token_file.write_text(answer.read().decode())
    identity = WebIdentity(token_file=token_file, endpoint=endpoint, region="us-east-1", role_arn="arn:aws:iam::000000000000:role/rask")
    producer = StorageOptions(key_pair=exchange(identity, session_name="doors", duration_seconds=900).key_pair, options={}).with_endpoint(endpoint)
    uri = f"s3://{bucket}/{OUTBOX}"
    runs = {name: f"{name}-{tag}" for name in ("ob-1", "ob-2", "ob-hidden", "ob-poison", "pk-1", "pk-2", "pk-3", "pk-4")}
    for name, outputs in (("ob-1", ["b"]), ("ob-2", ["b"]), ("ob-hidden", ["c"])):
        outbox.stage_envelope(uri, producer, _envelope(_run(runs[name], outputs, ["a"], author="admin", stamped=True)))
        time.sleep(0.01)
    outbox.stage_event(uri, producer, runs["ob-poison"], "{not an event")
    store = ProvenanceStore.from_settings(LineageSettings())
    parked = {"pk-1": ("admin", ["b"]), "pk-2": ("ada", ["c"]), "pk-3": ("admin", ["b"])}
    for name, (author, outputs) in parked.items():
        envelope = _envelope(_run(runs[name], outputs, author=author, stamped=True)).as_wire()
        row = store.record_refusal(
            outbox_key=f"dead_letter:{envelope['id']}", run_id=runs[name], author=f"idp~{author}", reason="denied", event_json=json.dumps(envelope)
        )
        asyncio.run(row)
    poison = json.dumps({"id": f"poison-{tag}", "data": {"not": "an event"}})
    asyncio.run(store.record_refusal(outbox_key=f"dead_letter:poison-{tag}", run_id=runs["pk-4"], author=None, reason="unreadable", event_json=poison))
    return runs


class Corpus:
    """The requests put to both services and what differed, by case."""

    def __init__(self, python_url: str, rust_url: str, issuer_url: str) -> None:
        import httpx

        self.clients = {"python": httpx.Client(base_url=python_url, timeout=120), "rust": httpx.Client(base_url=rust_url, timeout=120)}
        self.issuer_url = issuer_url
        self.tokens: dict[str, str] = {}
        self.differences: list[str] = []
        self.kept: list[str] = []
        self.by_operation: Counter[str] = Counter()
        self.writes = 0
        #: Each case's status as the Python app answered it, so a reader sees what the corpus reached.
        self.statuses: Counter[str] = Counter()

    def bearer(self, who: str | None) -> dict[str, str]:
        import httpx

        if who is None:
            return {}
        if who.startswith("!"):
            return {"authorization": who[1:]}
        if who not in self.tokens:
            self.tokens[who] = httpx.get(f"{self.issuer_url}/token", params={"sub": who}, timeout=10).text
        return {"authorization": f"Bearer {self.tokens[who]}"}

    def ask(self, side: str, method: str, path: str, who: str | None, body: Body = None, headers: dict[str, str] | None = None) -> Any:  # noqa: ANN401 - httpx's response
        sent = self.bearer(who) | (headers or {})
        content = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        if isinstance(body, (dict, list)):
            sent.setdefault("content-type", "application/json")
        return self.clients[side].request(method, path, headers=sent, content=content)

    def compare(self, name: str, answers: dict[str, Any], same: dict[str, str] | None = None) -> dict[str, Any]:
        """Compare the two answers; ``same`` maps a text in the Rust answer to the Python's it stands for."""
        seen = {}
        for side, answer in answers.items():
            text = answer.text
            for theirs, ours in (same or {}).items():
                text = text.replace(ours, theirs) if side == "rust" else text
            body = json.loads(text, object_pairs_hook=_ordered) if text else None
            if isinstance(body, dict):
                # A trace is named, never compared, and a backlog's age is the time the read was made.
                body["instance"] = "instance" in body
                if isinstance(body.get("oldest_age_seconds"), float):
                    body["oldest_age_seconds"] = "a float"
            headers = {header: answer.headers.get(header, "").replace(",HEAD", "") for header in HEADERS}
            seen[side] = {"status": answer.status_code, "headers": headers, "body": body}
        if seen["python"] != seen["rust"]:
            width = None if os.environ.get("DOORS_FULL") else 900
            line = f"{name}:\n    python: {json.dumps(seen['python'])[:width]}\n    rust:   {json.dumps(seen['rust'])[:width]}"
            (self.kept if name in KEPT else self.differences).append(line)
        self.statuses[f"{seen['python']['status']}"] += 1
        if os.environ.get("DOORS_VERBOSE"):
            detail = seen["python"]["body"].get("detail") if isinstance(seen["python"]["body"], dict) else None
            print(f"  {seen['python']['status']} {name}{f': {detail}' if detail else ''}", flush=True)
        return seen["python"]

    def both(
        self, name: str, method: str, path: str, who: str | None = "admin", body: Body = None, headers: dict[str, str] | None = None, first: str = "python"
    ) -> dict[str, Any]:
        self.by_operation[f"{method} {_template(path)}"] += 2
        order = ["python", "rust"] if first == "python" else ["rust", "python"]
        answers = {side: self.ask(side, method, path, who, body, headers) for side in order}
        return self.compare(name, answers)

    def paired(self, name: str, method: str, paths: tuple[str, str], same: dict[str, str], who: str = "admin") -> None:
        self.by_operation[f"{method} {_template(paths[0])}"] += 2
        answers = {"python": self.ask("python", method, paths[0], who), "rust": self.ask("rust", method, paths[1], who)}
        self.compare(name, answers, same)

    def pages(self, name: str, path: str, who: str, cursor: str, limit: int) -> None:
        """Page ``path`` to its end on each service, each following its own cursor, compared page by page."""
        marks = {"python": None, "rust": None}
        for page in range(40):
            answers = {}
            for side in ("python", "rust"):
                mark = marks[side]
                query = f"limit={limit}" + (f"&{cursor}={mark}" if mark is not None else ("&since=0" if cursor == "since" else ""))
                self.by_operation[f"GET {_template(path)}"] += 1
                answers[side] = self.ask(side, "GET", f"{path}{'&' if '?' in path else '?'}{query}", who)
            python = self.compare(f"{name}, page {page}", answers)
            if python["status"] != 200:
                return
            for side, answer in answers.items():
                marks[side] = answer.json().get("next_cursor")
            if marks["python"] is None or (cursor == "since" and not answers["python"].json()["events"]):
                return


def _ordered(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """An object with the order its members were written in, which a comparison then includes."""
    return dict(pairs) | {"(order)": [key for key, _ in pairs]}


def _template(path: str) -> str:
    path = path.split("?", 1)[0]
    for prefix, template in (("/tables/", "/tables/{name}/derived"), ("/admin/dlq/parked/", "/admin/dlq/parked/{run_id}/replay")):
        if path.startswith(prefix) and path.endswith("/replay" if "dlq" in prefix else "/derived"):
            return template
    if path.startswith("/admin/dlq/") and path.endswith("/replay"):
        return "/admin/dlq/{run_id}/replay"
    return path


def corpus(corpus: Corpus, runs: dict[str, str]) -> None:
    lineage = "/api/v1/lineage"
    unauthenticated = [None, "!Bearer not-a-token", "!Basic YWRhOnB3", "!bearer"]
    doors = ["GET /events", "GET /events/projection", f"GET /tables/{table('a')}/derived", "GET /admin/dlq", "POST /admin/dlq/x/replay"]
    doors += ["GET /admin/dlq/parked", "POST /admin/dlq/parked/x/replay", f"POST {lineage}"]
    for door in doors:
        method, path = door.split(" ")
        for who in unauthenticated:
            corpus.both(f"{door} as {who}", method, path, who)
    for query in ("limit=0", "limit=501", "limit=x", "after=0", "since=-1", "after=1&since=1", "summary=maybe", "table=", "namespace=", "limit=5_0&summary=2"):
        corpus.both(f"events?{query}", "GET", f"/events?{query}", "ada")
        corpus.both(f"projection?{query}", "GET", f"/events/projection?{query}", "feed")
    for query in ("limit=0", "limit=501", "limit=abc"):
        corpus.both(f"dlq?{query}", "GET", f"/admin/dlq?{query}", "admin")
    for query in ("depth=0", "depth=21", "depth=x"):
        corpus.both(f"derived?{query}", "GET", f"/tables/{table('a')}/derived?{query}", "ada")
    for who in ("ada", "bob", "feed"):
        corpus.both(f"projection as {who}", "GET", "/events/projection", who)
        corpus.both(f"parked as {who}", "GET", "/admin/dlq/parked", who)
        corpus.both(f"parked replay as {who}", "POST", f"/admin/dlq/parked/{runs['pk-2']}/replay", who)
    corpus.both("derived of a table the caller may not read", "GET", f"/tables/{table('c')}/derived", "ada")
    corpus.both("derived of a table the catalog does not know", "GET", f"/tables/{table('zz')}/derived", "ada")
    corpus.both("derived of a name with an encoded slash", "GET", "/tables/a%2Fb/derived", "ada")
    # Names the catalog refuses as none (a segment holding a slash, an empty segment): the caller's own input, refused 400.
    corpus.both("derived of a name with an empty segment", "GET", f"/tables/{table('')}/derived", "ada")
    corpus.both("events of a table with a slash", "GET", f"/events?table={urllib.parse.quote(table('x/y'), safe='')}", "ada")
    corpus.both("events of a namespace with an empty segment", "GET", f"/events?namespace={urllib.parse.quote(DATA + '$', safe='')}", "ada")
    corpus.both("no door at the path", "GET", "/events/nope", "ada")
    corpus.both("no door for the method", "DELETE", "/events", "ada")
    corpus.both("a trailing slash", "GET", "/admin/dlq/", "ada")
    corpus.both("HEAD on a GET door", "HEAD", "/events/projection", "feed")

    bodies = {
        "ingest: no body": (None, {}),
        "ingest: JSON that does not parse": (b"{bad", {"content-type": "application/json"}),
        "ingest: a trailing comma": (b'{"a": 1,}', {"content-type": "application/json"}),
        "ingest: a list": (b"[]", {"content-type": "application/json"}),
        "ingest: null": (b"null", {"content-type": "application/json"}),
        "ingest: text/plain": (b"{}", {"content-type": "text/plain"}),
        "ingest: no content type": (b"{}", {}),
        "ingest: a body over the cap": (b"x" * 70_000, {"content-type": "application/json"}),
        "ingest: a body no event model takes": (b"{}", {"content-type": "application/json"}),
        "ingest: bytes that are no UTF-8": (b'{"a": "\xff"}', {"content-type": "application/json"}),
        "ingest: nested deeper than the reader reads": (b"[" * 600 + b"]" * 600, {"content-type": "application/json"}),
        "ingest: nested far deeper": (b"[" * 3000 + b"]" * 3000, {"content-type": "application/json"}),
    }
    for name, (body, headers) in bodies.items():
        corpus.both(name, "POST", lineage, "ada", body, headers)
    job_less = _run("no-job", ["b"])
    del job_less["job"]
    corpus.both("ingest: a run event with no job", "POST", lineage, "ada", job_less)

    writes: list[tuple[str, str, dict[str, Any]]] = [
        ("admin writes a run reading a into b", "admin", _run("r-1", ["b"], ["a"])),
        ("admin starts a run reading b into c", "admin", _run("r-2", ["c"], ["b"], state="START")),
        ("ada writes a run reading a into b", "ada", _run("r-3", ["b"], ["a"])),
        ("ada names an input she may not read", "ada", _run("r-4", ["b"], ["c"])),
        ("ada writes a table she may not write", "ada", _run("r-5", ["c"])),
        ("ada extends admin's run", "ada", _run("r-1", ["b"], ["a"], state="FAIL")),
        ("ada changes b with no run", "ada", _static("b")),
        ("bob changes a with no run", "bob", _static("a")),
        (
            "admin writes b with column lineage from c",
            "admin",
            _run("r-6", ["b"], [], columnLineage={"fields": {"id": {"inputFields": [_dataset("c") | {"field": "id"}]}}}),
        ),
        ("admin reads an outside source into a", "admin", _run("r-7", ["a"], ["s3://raw/x"])),
        ("ada writes an outside location", "ada", _run("r-8", ["s3://raw/out"])),
        ("admin sends a claimed signature", "admin", _run("r-9", ["a"]) | {"run": {"runId": "r-9", "facets": {"rask_signature": {"kid": "x"}}}}),
        ("admin writes a table the catalog does not know", "admin", _run("r-10", ["zz"])),
        ("admin names a governed table with a slash", "admin", _run("r-11", ["a"], ["x/y"])),
        ("admin writes a governed table with a slash", "admin", _run("r-12", ["x/y"])),
    ]
    writes += [(f"admin writes run {n} into a", "admin", _run(f"p-{n}", ["a"])) for n in range(14)]
    for index, (name, who, event) in enumerate(writes):
        corpus.writes += 1
        corpus.both(f"ingest: {name}", "POST", lineage, who, event, first="python" if index % 2 == 0 else "rust")

    for who in ("admin", "ada", "bob"):
        corpus.pages(f"events as {who}, older", "/events", who, "after", 4)
        corpus.pages(f"events as {who}, forward", "/events", who, "since", 5)
        corpus.both(f"events as {who}, a summary", "GET", "/events?summary=true&limit=3", who)
        corpus.both(f"events as {who}, scoped to a", "GET", f"/events?table={table('a')}&limit=6", who)
        corpus.both(f"events as {who}, scoped to b, summary", "GET", f"/events?table={table('b')}&summary=1", who)
        corpus.both(f"events as {who}, scoped to the namespace", "GET", f"/events?namespace={DATA}&limit=7", who)
        corpus.both(f"events as {who}, scoped to a name the catalog does not know", "GET", f"/events?table={table('zz')}", who)
        corpus.both(f"events as {who}, since past the head", "GET", "/events?since=100000", who)
        corpus.both(f"events as {who}, since past 64 bits", "GET", "/events?since=99999999999999999999", who)
        for depth in ("", "?depth=1"):
            corpus.both(f"derived of a as {who}{depth}", "GET", f"/tables/{table('a')}/derived{depth}", who)
        corpus.both(f"dlq as {who}", "GET", "/admin/dlq", who)
        corpus.both(f"dlq as {who}, limit 2", "GET", "/admin/dlq?limit=2", who)
    corpus.pages("projection, older", "/events/projection", "feed", "after", 6)
    corpus.pages("projection, forward", "/events/projection", "feed", "since", 9)
    corpus.both("projection past the head", "GET", "/events/projection?since=100000", "feed")
    corpus.both("parked as admin", "GET", "/admin/dlq/parked", "admin")

    corpus.both("replay of a run nothing staged", "POST", "/admin/dlq/nothing/replay", "admin")
    corpus.both("replay of a poison object", "POST", f"/admin/dlq/{runs['ob-poison']}/replay", "admin")
    corpus.both("replay of a staged event the caller may not see", "POST", f"/admin/dlq/{runs['ob-hidden']}/replay", "ada")
    corpus.both("replay of a staged event bob may not see", "POST", f"/admin/dlq/{runs['ob-1']}/replay", "bob")
    corpus.both("replay of a staged run by an operator who may not write it", "POST", f"/admin/dlq/{runs['ob-hidden']}/replay", "feed")
    corpus.paired("replay of a staged run", "POST", (f"/admin/dlq/{runs['ob-1']}/replay", f"/admin/dlq/{runs['ob-2']}/replay"), {runs["ob-1"]: runs["ob-2"]})
    corpus.both("dlq after the replays", "GET", "/admin/dlq", "admin")
    corpus.both("parked replay of a run nothing kept", "POST", "/admin/dlq/parked/nothing/replay", "admin")
    corpus.both("parked replay still refused", "POST", f"/admin/dlq/parked/{runs['pk-2']}/replay", "admin")
    corpus.both("parked replay of a delivery no event parses out of", "POST", f"/admin/dlq/parked/{runs['pk-4']}/replay", "admin")
    corpus.paired(
        "parked replay recovered",
        "POST",
        (f"/admin/dlq/parked/{runs['pk-1']}/replay", f"/admin/dlq/parked/{runs['pk-3']}/replay"),
        {runs["pk-1"]: runs["pk-3"]},
    )
    corpus.both("parked after the replays", "GET", "/admin/dlq/parked", "admin")
    corpus.pages("events after the replays", "/events", "admin", "after", 50)


def main() -> None:
    command, *rest = sys.argv[1:]
    if command == "issuer":
        issuer(int(rest[0]))
    elif command == "setup":
        setup(Path(rest[0]), *rest[1:])
    else:
        work, python_url, rust_url, issuer_url, endpoint, bucket, store_issuer = rest
        runs = _seed(endpoint, bucket, store_issuer, Path(work))
        checked = Corpus(python_url, rust_url, issuer_url)
        corpus(checked, runs)
        total = sum(checked.by_operation.values())
        print(f"{total} requests ({total // 2} each), {checked.writes} writes put to both; cases by status: {dict(sorted(checked.statuses.items()))}")
        for operation, count in sorted(checked.by_operation.items()):
            print(f"  {count:4d}  {operation}")
        for line in checked.kept:
            print(f"KEPT {line}")
        for line in checked.differences:
            print(f"DIFF {line}")
        print(f"{len(checked.differences)} difference(s), {len(checked.kept)} kept on purpose (ADR 0236)")
        sys.exit(1 if checked.differences else 0)


if __name__ == "__main__":
    main()
