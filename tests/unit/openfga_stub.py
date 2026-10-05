"""A stub OpenFGA that validates each tuple against the model a request names, the way OpenFGA does ([[LH-201]]).

A tuple request that names no `authorization_model_id` is validated against the store's NEWEST model. The
history served here is [a legacy body without `estate` (newest), this checkout's `model.json`], one model per
page: the shape the LH-201 review reproduced on OpenFGA v1.18.3, where an image that still provisions its own
narrower model is the newest and every `estate:rask` request naming no model, or naming the newest, is refused
`type 'estate' not found`. A reader that stops at page one never sees this checkout's model.

`STORE` is the one a caller must use; every other listed store answers 404. By name it is the newest
`lance-catalog`; pinned, it is named otherwise and a NEWER `lance-catalog` exists beside it.

Every request must carry ``Authorization: Bearer <the token file's content at that moment>``, or it answers 401
as an OpenFGA with OIDC authn does ([[XC-077]]): a client that sends no credential, or keeps the token it read
before the file was rotated, is refused.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, Field

from service_kit.governed.auth.write_model import STORE_NAME, model_document


STORE: Final = "01J00000000000000000000ST1"
STORE_OLDER: Final = "01J00000000000000000000ST0"
STORE_NEWER: Final = "01J00000000000000000000ST2"
CARRYING: Final = "01J00000000000000000000CAR"
LEGACY: Final = "01J00000000000000000000GCY"

BY_NAME: Final = [
    {"id": STORE_OLDER, "name": STORE_NAME, "created_at": "2026-07-15T00:00:00Z", "updated_at": "2026-07-15T00:00:00Z"},
    {"id": STORE, "name": STORE_NAME, "created_at": "2026-07-15T00:00:01Z", "updated_at": "2026-07-15T00:00:01Z"},
]
PINNED: Final = [
    {"id": STORE, "name": "pinned", "created_at": "2026-07-15T00:00:00Z", "updated_at": "2026-07-15T00:00:00Z"},
    {"id": STORE_NEWER, "name": STORE_NAME, "created_at": "2026-07-15T00:00:01Z", "updated_at": "2026-07-15T00:00:01Z"},
]


def legacy_body() -> dict[str, Any]:
    """The checkout's model without its `estate` type: what an older image that still provisions writes."""
    body = model_document()
    body["type_definitions"] = [t for t in body["type_definitions"] if t["type"] != "estate"]
    return body


class Recorded(BaseModel):
    """What the stub saw: every check and write request body, the tuples the store holds, and how many requests it refused 401."""

    requests: list[dict[str, Any]] = Field(default_factory=list)
    written: set[tuple[str, str, str]] = Field(default_factory=set)
    refused: int = 0


def _handler(stores: list[dict[str, str]], recorded: Recorded, token_file: Path) -> type[BaseHTTPRequestHandler]:
    history: list[dict[str, Any]] = [{"id": LEGACY, **legacy_body()}, {"id": CARRYING, **model_document()}]

    class OpenFga(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _refused(self) -> bool:
            if self.headers.get("authorization") == f"Bearer {token_file.read_text().strip()}":
                return False
            recorded.refused += 1
            self._send(401, {"code": "unauthenticated", "message": "unauthenticated"})
            return True

        def do_GET(self) -> None:
            if self._refused():
                return None
            url = urlsplit(self.path)
            if url.path == "/stores":
                return self._send(200, {"stores": stores, "continuation_token": ""})
            if url.path != f"/stores/{STORE}/authorization-models":
                return self._send(404, {"code": "store_id_not_found"})
            page = int(parse_qs(url.query).get("continuation_token", ["0"])[0])
            more = page + 1 < len(history)
            return self._send(200, {"authorization_models": history[page : page + 1], "continuation_token": str(page + 1) if more else ""})

        def do_POST(self) -> None:
            if self._refused():
                return None
            verb = urlsplit(self.path).path.removeprefix(f"/stores/{STORE}/")
            if verb not in ("check", "read", "write"):
                return self._send(404, {"code": "store_id_not_found"})
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            if verb == "read":
                # Read takes no model: it returns the stored tuples on an object, whatever validated them.
                obj = body["tuple_key"]["object"]
                held = [{"key": {"user": u, "relation": r, "object": o}} for u, r, o in sorted(recorded.written) if o == obj]
                return self._send(200, {"tuples": held, "continuation_token": ""})
            recorded.requests.append(body)
            named = body.get("authorization_model_id") or history[0]["id"]
            model = next((m for m in history if m["id"] == named), None)
            if model is None:
                return self._send(400, {"code": "authorization_model_not_found", "message": named})
            keys = [body["tuple_key"]] if verb == "check" else body["writes"]["tuple_keys"]
            for key in keys:
                type_name = key["object"].split(":", 1)[0]
                relations = next((t.get("relations") or {} for t in model["type_definitions"] if t["type"] == type_name), None)
                if relations is None:
                    return self._send(400, {"code": "validation_error", "message": f"type '{type_name}' not found"})
                if key["relation"] not in relations:
                    return self._send(400, {"code": "validation_error", "message": f"relation '{type_name}#{key['relation']}' not found"})
            triples = {(k["user"], k["relation"], k["object"]) for k in keys}
            if verb == "check":
                return self._send(200, {"allowed": triples <= recorded.written})
            if held := sorted(triples & recorded.written):
                # A write naming a stored tuple fails whole (`test_fga_resilience.py`'s `_TransactionalStore`).
                user, relation, obj = held[0]
                message = f"cannot write a tuple which already exists: user: '{user}', relation: '{relation}', object: '{obj}': invalid write input"
                return self._send(400, {"code": "write_failed_due_to_invalid_input", "message": message})
            recorded.written |= triples
            return self._send(200, {})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — the stdlib's own parameter name
            del format, args

    return OpenFga


@contextmanager
def openfga(stores: list[dict[str, str]], recorded: Recorded, *, token_file: Path) -> Iterator[str]:
    """Serve the stub on a free local port for the block, admitting the bearer ``token_file`` holds; yields its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(stores, recorded, token_file))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
