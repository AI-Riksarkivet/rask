"""The door vectors of ``gen_port_vectors.py``: what the Python lineage app answers each request of a corpus.

The app is assembled as ``lineage.main`` assembles it (``build_lance_service_app`` with lineage's routers and limits,
and the judge before the body), with authentication and authorization on. Only its collaborators are stand-ins, so an
answer depends on nothing outside the vector: each verifier holds one recorded key, the decider answers from the
grants below, the catalog's names resolve from the table below, no outbox or bus is configured, and the provenance store
refuses as a catalog that does not answer would. A Rust test builds the same stand-ins and replays every request
through the Rust router (`rust/crates/lineage/tests/doors_vectors.rs`); ``scripts/port_doors_check.sh`` puts the same
doors to both implementations over real collaborators.

A bearer is written ``@<subject>`` (a token the recorded key signs for that subject, valid now) or ``@<subject>:expired``;
anything else is sent as it is.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


#: The secret the recorded tokens are signed with and the key id they name: a fixture's, protecting nothing.
TOKEN_SECRET = "recorded-door-tokens-only-fixture-secret"  # noqa: S105 - named as a fixture in the vector file itself
KID = "k1"
ISSUER = "https://idp.example"
AUDIENCE = "lance"
CATALOG = "lance-namespace://localhost/api/catalog"
ROOT = "estate:rask"
MAX_BODY_BYTES = 4096
_UA = "0190a1b2-0000-7000-8000-0000000000a1"
_UB = "0190a1b2-0000-7000-8000-0000000000b2"
_UNS = "0190a1b2-0000-7000-8000-0000000000ff"
#: What each subject may do, as ``relation object`` pairs; the admin holds every estate rung.
GRANTS: dict[str, list[str]] = {
    "idp~admin": [f"{relation} {ROOT}" for relation in ("can_read_event_feed", "can_read_parked_events", "can_replay_parked_events")]
    + [f"{relation} table:{uuid}" for relation in ("can_get_metadata", "can_write_data") for uuid in (_UA, _UB)],
    "idp~ada": [f"can_get_metadata table:{_UA}", f"can_write_data table:{_UB}"],
    "idp~bob": [],
}
#: The tables and namespaces the catalog names, by name: a table's uuid and its namespace's.
TABLES: dict[str, list[str]] = {"wh$ns$a": [_UA, _UNS], "wh$ns$b": [_UB, _UNS]}
NAMESPACES: dict[str, str] = {"wh$ns": _UNS}
#: The names the catalog's describe doors refuse 400, as it refuses a name with a segment that is no name.
REFUSED: list[str] = ["wh$$a", "wh$"]

ENV = {
    "RASK_OIDC_ENABLED": "true",
    "RASK_OIDC_ISSUER": ISSUER,
    "RASK_OIDC_AUDIENCE": AUDIENCE,
    "RASK_OIDC_IDP_ID": "idp",
    "RASK_FGA_ENABLED": "true",
    "RASK_FGA_STORE_ID": "store",
    "RASK_FGA_MODEL_ID": "model",
    "RASK_FGA_ROOT_OBJECT": ROOT,
    "LINEAGE_CATALOG_URL": "http://127.0.0.1:9",
    "LINEAGE_CATALOG_DATASET_NAMESPACE": CATALOG,
    "LINEAGE_MAX_BODY_BYTES": str(MAX_BODY_BYTES),
}


def _event(outputs: list[str], inputs: Sequence[str] = (), **members: Any) -> str:  # noqa: ANN401 - an event is arbitrary JSON
    import json

    def dataset(name: str) -> dict[str, Any]:
        return {"namespace": CATALOG, "name": name}

    run = {
        "eventType": "COMPLETE",
        "eventTime": "2026-11-17T10:00:00Z",
        "run": {"runId": "run-1"},
        "job": {"namespace": "jobs", "name": "copy"},
        "inputs": [dataset(name) for name in inputs],
        "outputs": [dataset(name) for name in outputs],
        "producer": "https://example/producer",
    }
    return json.dumps(run | members)


def _static(name: str) -> str:
    import json

    return json.dumps({"eventTime": "2026-11-17T10:00:00Z", "producer": "https://example/producer", "dataset": {"namespace": CATALOG, "name": name}})


def requests() -> list[dict[str, Any]]:
    """The corpus: a name, the method and path, the headers, and the body (text, or a size sent declared or not)."""
    ada, admin = {"authorization": "@ada"}, {"authorization": "@admin"}
    as_json = {"content-type": "application/json"}

    def case(name: str, method: str, path: str, headers: dict[str, str] | None = None, body: Any = None) -> dict[str, Any]:  # noqa: ANN401
        return {"name": name, "method": method, "path": path, "headers": ada if headers is None else headers, "body": body}

    doors = [
        ("GET", "/events"),
        ("GET", "/events/projection"),
        ("GET", "/tables/wh$ns$a/derived"),
        ("GET", "/admin/dlq"),
        ("POST", "/admin/dlq/run-1/replay"),
        ("GET", "/admin/dlq/parked"),
        ("POST", "/admin/dlq/parked/run-1/replay"),
        ("POST", "/api/v1/lineage"),
    ]
    callers = [
        ("no bearer", {}),
        ("a bearer nobody signed", {"authorization": "Bearer not-a-token"}),
        ("another scheme", {"authorization": "Basic YWRhOnB3"}),
        ("an empty bearer", {"authorization": "Bearer   "}),
        ("an expired bearer", {"authorization": "@ada:expired"}),
    ]
    unauthenticated = [case(f"{method} {path}: {name}", method, path, headers) for method, path in doors for name, headers in callers]
    queries = [
        ("limit 0", "limit=0"),
        ("limit 501", "limit=501"),
        ("limit is no integer", "limit=abc"),
        ("an empty limit", "limit="),
        ("a limit with an exponent", "limit=1e1"),
        ("a limit with a fraction", "limit=5.5"),
        ("a limit past CPython's digits", "limit=" + "1" * 4301),
        ("after 0", "after=0"),
        ("since below 0", "since=-1"),
        ("since past 64 bits, then limit 0", "since=99999999999999999999&limit=0"),
        ("after is no integer", "after=x"),
        ("an empty table", "table="),
        ("an empty namespace", "namespace="),
        ("summary is no boolean", "summary=maybe"),
        ("summary written empty", "summary="),
        ("every field wrong, in declared order", "summary=maybe&limit=abc&table=&since=-1&after=x"),
        ("the last value of a name wins", "limit=5&limit=0"),
        ("a plus is a space", "limit=+0"),
        ("an underscore between digits, then a bad summary", "limit=1_0&summary=2"),
        ("after and since", "after=3&since=2"),
        ("after and since, written as text a reader trims", "after=%203%20&since=+2.00"),
        ("a name with no value", "summary"),
    ]
    events = [case(f"GET /events: {name}", "GET", f"/events?{query}") for name, query in queries]
    projection = [
        case("GET /events/projection: limit 0", "GET", "/events/projection?limit=0"),
        case("GET /events/projection: after and since", "GET", "/events/projection?after=1&since=1"),
        case("GET /events/projection: after and since, as the admin", "GET", "/events/projection?after=1&since=1", admin),
        case("GET /events/projection: a reader without the rung", "GET", "/events/projection"),
        case("GET /events/projection: a subject with no grant", "GET", "/events/projection?since=0", {"authorization": "@bob"}),
        case("GET /events/projection: the admin reaches the feed", "GET", "/events/projection", admin),
        case("GET /events/projection: a table is no member of it", "GET", "/events/projection?table=", admin),
    ]
    reaching = [
        case("GET /events: a reader reaches the feed", "GET", "/events"),
        case("GET /events: scoped to a table the catalog names", "GET", "/events?table=wh$ns$a&summary=true"),
        case("GET /events: scoped to a namespace", "GET", "/events?namespace=wh$ns"),
        case("GET /events: scoped to a table the catalog refuses as no name", "GET", "/events?table=wh%24%24a"),
        case("GET /events: scoped to a namespace the catalog refuses as no name", "GET", "/events?namespace=wh%24"),
    ]
    tables = [
        case("GET derived: a table the caller may read", "GET", "/tables/wh$ns$a/derived"),
        case("GET derived: depth 0 after the gate passes", "GET", "/tables/wh$ns$a/derived?depth=0"),
        case("GET derived: depth 21", "GET", "/tables/wh$ns$a/derived?depth=21"),
        case("GET derived: a table the caller may not read, depth 0", "GET", "/tables/wh$ns$b/derived?depth=0"),
        case("GET derived: a name the catalog does not know", "GET", "/tables/wh$ns$zz/derived"),
        case("GET derived: a name with an encoded slash", "GET", "/tables/wh%2Fns/derived"),
        case("GET derived: a percent-encoded name", "GET", "/tables/wh%24ns%24a/derived?depth=1"),
        case("GET derived: a name the catalog refuses as no name", "GET", "/tables/wh$$a/derived"),
    ]
    dlq = [
        case("GET /admin/dlq: no outbox", "GET", "/admin/dlq"),
        case("GET /admin/dlq: no outbox, the limit echoed", "GET", "/admin/dlq?limit=7"),
        case("GET /admin/dlq: limit 0", "GET", "/admin/dlq?limit=0"),
        case("GET /admin/dlq: limit 501", "GET", "/admin/dlq?limit=501"),
        case("POST replay: no outbox", "POST", "/admin/dlq/run-1/replay"),
        case("POST replay: a run id with a query", "POST", "/admin/dlq/run%201/replay?x=1"),
        case("POST replay: the run named parked", "POST", "/admin/dlq/parked/replay"),
        case("GET parked: a reader without the rung", "GET", "/admin/dlq/parked"),
        case("GET parked: the admin reaches the store", "GET", "/admin/dlq/parked", admin),
        case("POST parked replay: a reader without the rung", "POST", "/admin/dlq/parked/run-1/replay"),
        case("POST parked replay: the admin reaches the store", "POST", "/admin/dlq/parked/run-1/replay", admin),
    ]
    body = [
        case("ingest: no body", "POST", "/api/v1/lineage"),
        case("ingest: an empty JSON body", "POST", "/api/v1/lineage", ada | as_json, ""),
        case("ingest: JSON that does not parse", "POST", "/api/v1/lineage", ada | as_json, "{bad"),
        case("ingest: a trailing comma", "POST", "/api/v1/lineage", ada | as_json, '{"a": 1,}'),
        case("ingest: an error past text outside ASCII", "POST", "/api/v1/lineage", ada | as_json, '{"ö": "å", }'),
        case("ingest: extra data", "POST", "/api/v1/lineage", ada | as_json, "{} {}"),
        case("ingest: a list", "POST", "/api/v1/lineage", ada | as_json, "[]"),
        case("ingest: null", "POST", "/api/v1/lineage", ada | as_json, "null"),
        case("ingest: a string", "POST", "/api/v1/lineage", ada | as_json, '"x" '),
        case("ingest: text/plain", "POST", "/api/v1/lineage", ada | {"content-type": "text/plain"}, "{}"),
        case("ingest: no content type", "POST", "/api/v1/lineage", ada, "{}"),
        case("ingest: a JSON suffix type", "POST", "/api/v1/lineage", ada | {"content-type": "application/vnd.x+json; charset=utf-8"}, "[1]"),
        case("ingest: a content type with two slashes", "POST", "/api/v1/lineage", ada | {"content-type": "application/json/x"}, "{}"),
        case("ingest: an upper-case content type", "POST", "/api/v1/lineage", ada | {"content-type": "APPLICATION/JSON"}, "[]"),
        case("ingest: a byte-order mark", "POST", "/api/v1/lineage", ada | as_json, "﻿[]"),
        case("ingest: a body at the cap", "POST", "/api/v1/lineage", ada | as_json, {"size": MAX_BODY_BYTES, "declared": True}),
        case("ingest: a body declared over the cap", "POST", "/api/v1/lineage", ada | as_json, {"size": MAX_BODY_BYTES + 1, "declared": True}),
        case("ingest: a body over the cap, undeclared", "POST", "/api/v1/lineage", ada | as_json, {"size": MAX_BODY_BYTES + 1, "declared": False}),
        case("ingest: over the cap with no bearer", "POST", "/api/v1/lineage", as_json, {"size": MAX_BODY_BYTES + 1, "declared": True}),
        case("ingest: over the cap undeclared with no bearer", "POST", "/api/v1/lineage", as_json, {"size": MAX_BODY_BYTES + 1, "declared": False}),
    ]
    gated = [
        case("ingest: a static change to a table the catalog does not know", "POST", "/api/v1/lineage", ada | as_json, _static("wh$ns$zz")),
        case("ingest: a static change to a table without the writer's rung", "POST", "/api/v1/lineage", ada | as_json, _static("wh$ns$a")),
        case("ingest: a static change by a subject with no grant", "POST", "/api/v1/lineage", {"authorization": "@bob"} | as_json, _static("wh$ns$b")),
        case("ingest: a static change the caller may write reaches the store", "POST", "/api/v1/lineage", ada | as_json, _static("wh$ns$b")),
        case("ingest: a run reaches the store for its run's author", "POST", "/api/v1/lineage", ada | as_json, _event(["wh$ns$b"], ["wh$ns$a"])),
        case("ingest: an external output", "POST", "/api/v1/lineage", ada | as_json, _static("s3://bucket/raw")),
        case("ingest: an output the catalog refuses as no name", "POST", "/api/v1/lineage", ada | as_json, _event(["wh$$a"], ["wh$ns$a"])),
    ]
    framework = [
        case("no door at the path", "GET", "/events/nope"),
        case("no door for the method", "POST", "/events"),
        case("no door for the method, no bearer", "DELETE", "/admin/dlq", {}),
        case("GET on the ingest door", "GET", "/api/v1/lineage"),
        case("a trailing slash", "GET", "/admin/dlq/"),
    ]
    return unauthenticated + events + projection + reaching + tables + dlq + body + gated + framework


def _settings() -> Any:  # noqa: ANN401 - the service's settings
    from lineage.core.config import LineageSettings

    with patch.dict(os.environ, ENV, clear=True):
        return LineageSettings()


def _app(settings: Any) -> Any:  # noqa: ANN401 - the FastAPI app
    import logging
    from collections.abc import AsyncGenerator
    from contextlib import asynccontextmanager

    import jwt
    from fastapi import FastAPI, Request
    from lance_namespace import ServiceUnavailableError
    from lance_namespace_urllib3_client.exceptions import BadRequestException

    from lakehouse.admission import judge_before_the_body
    from lakehouse.governed import idp_identity, oidc
    from lakehouse.governed.decider import TableDecision
    from lakehouse.lance_app import build_lance_service_app
    from lakehouse.lifecycle import mark_started
    from lakehouse.lineage_events import DatasetUuids
    from lineage.api.security import authenticate, judge
    from lineage.api.v1.router import api_router
    from lineage.core.config import get_settings
    from lineage.services import names

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield

    async def ready(request: Request) -> Any:  # noqa: ANN401
        raise ServiceUnavailableError("not probed")

    app = build_lance_service_app(
        title="Lance Lineage Service",
        docs_enabled=False,
        audit_enabled=False,
        lifespan=lifespan,
        log=logging.getLogger("doors"),
        routers=[api_router],
        ready_check=ready,
        max_body_bytes=settings.max_body_bytes,
        request_deadline_seconds=settings.request_deadline_seconds,
    )
    judge_before_the_body(app, gate=authenticate, judge=judge)
    app.dependency_overrides[get_settings] = lambda: settings
    key = jwt.PyJWK({"kty": "oct", "k": base64.urlsafe_b64encode(TOKEN_SECRET.encode()).rstrip(b"=").decode(), "kid": KID, "alg": "HS256"})
    keys: Any = SimpleNamespace(
        uri="https://recorded/keys", jwk_set_cache=None, get_signing_keys=lambda refresh=False: [key], match_kid=jwt.PyJWKClient.match_kid
    )
    verifier = oidc.OIDCVerifier([ISSUER], AUDIENCE, 300, leeway=60)
    verifier._held[ISSUER] = oidc._Provider(spec=oidc._Discovery(issuer=ISSUER, jwks_uri="https://recorded/keys"), jwk_client=keys, algorithms=["HS256"])
    app.state.oidc = idp_identity.IdpVerifier(verifier, settings.oidc_person_providers)
    app.state.sa_oidc = None

    def allowed(user: str, relation: str, obj: str) -> bool:
        return f"{relation} {obj}" in GRANTS.get(user, [])

    class Decider:
        resolves_names = False

        async def check(self, *, user: str, relation: str, obj: str) -> bool:
            return allowed(user, relation, obj)

        async def batch_check(self, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
            return {obj: allowed(user, relation, obj) for obj in objects}

        async def check_tables(self, *, user: str, relation: str, tables: list[str]) -> dict[str, TableDecision]:
            raise AssertionError("names are described, never decided, here")

        async def governed(self, objects: list[str]) -> dict[str, bool]:
            return dict.fromkeys(objects, True)

        async def locate(self, locations: list[str]) -> dict[str, str | None]:
            return dict.fromkeys(locations)

    def refuse(name: str) -> None:
        """The catalog's 400 for ``name``, through lineage's own translation of it."""
        if name in REFUSED:
            with names._described_as_the_callers(name):
                raise BadRequestException(status=400, reason="Bad Request", body='{"error": "the catalog\'s own words", "trace": "t-1"}')

    class Names:
        async def table(self, name: str, *, bearer: str | None) -> DatasetUuids:
            refuse(name)
            table, namespace = TABLES.get(name, [None, None])
            return DatasetUuids(table_uuid=table, namespace_uuid=namespace)

        async def namespace(self, name: str, *, bearer: str | None) -> str | None:
            refuse(name)
            return NAMESPACES.get(name)

    class Unanswered:
        """A provenance store whose catalog does not answer."""

        def __getattr__(self, name: str) -> Any:  # noqa: ANN401
            async def refuse(*_: object, **__: object) -> None:
                raise ServiceUnavailableError("the catalog is unavailable")

            return refuse

    app.state.decider = Decider()
    app.state.names = Names()
    app.state.provenance = Unanswered()
    app.state.bus = None
    mark_started(app)
    return app


def bearer_of(value: str, now: int) -> str:
    import jwt

    if not value.startswith("@"):
        return value
    subject, _, expired = value[1:].partition(":")
    claims = {"iss": ISSUER, "sub": subject, "aud": AUDIENCE, "exp": now + (-300 if expired else 300), "iat": now - 10}
    return "Bearer " + jwt.encode(claims, TOKEN_SECRET, algorithm="HS256", headers={"kid": KID})


async def _answers() -> list[dict[str, Any]]:
    import httpx

    app = _app(_settings())

    async def unsent(size: int) -> Any:  # noqa: ANN401 - an async generator of bytes
        yield b"x" * size

    vectors = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://lineage") as client:
        for case in requests():
            now = int(time.time())
            headers = {name: bearer_of(value, now) for name, value in case["headers"].items()}
            body = case["body"]
            if isinstance(body, dict):
                content: Any = b"x" * body["size"] if body["declared"] else unsent(body["size"])
            else:
                content = body.encode() if body is not None else None
            answer = await client.request(case["method"], case["path"], headers=headers, content=content)
            parsed = answer.json() if answer.content else None
            vectors.append(
                case
                | {
                    "status": answer.status_code,
                    "answered": {
                        name: answer.headers[name] for name in ("content-type", "cache-control", "vary", "allow", "location") if name in answer.headers
                    },
                    "answer": {key: value for key, value in parsed.items() if key != "instance"} if isinstance(parsed, dict) else parsed,
                    "instance": isinstance(parsed, dict) and "instance" in parsed,
                }
            )
    return vectors


def doors_vectors() -> dict[str, list[Any]]:
    """The stand-ins and every exchange, as ``gen_port_vectors.py`` writes them."""
    import logging

    logging.disable(logging.CRITICAL)
    try:
        exchanges = asyncio.run(_answers())
    finally:
        logging.disable(logging.NOTSET)
    stand_ins = {"secret": TOKEN_SECRET, "kid": KID, "env": ENV, "grants": GRANTS, "tables": TABLES, "namespaces": NAMESPACES, "refused": REFUSED}
    return {"stand_ins": [stand_ins], "exchanges": exchanges}
