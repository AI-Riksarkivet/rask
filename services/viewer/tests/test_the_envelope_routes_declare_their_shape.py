"""A route whose ENVELOPE is fixed must declare it in the schema (VS-18).

open_python-audit VS-18: ten routes annotated a bare `dict[str, Any]` / `list[dict[str, Any]]`, so
FastAPI serialized whatever Lance produced and OpenAPI documented "an object". The finding's own
recommendation draws the line this file draws: define the envelope, and keep an explicit
`dict[str, Any]` FIELD where the row shape is genuinely dataset-dependent.

WHICH ROUTES CAN AND CANNOT BE MODELLED, stated rather than left implicit:

* Envelope routes — `/api/atlas/status`, `/api/atlas/chunks/by-key`, `/api/doc-chunks/{doc_id}`,
  `/api/chunk-alignments/...` — carry a fixed structure around the rows. Those are pinned here.
  (`/api/documents` was the seventh viewer site and already models its envelope: `DocumentsPage`.)
* Bare-hit routes — `/api/atlas/chunk/...`, `/api/atlas/chunks`, and the search service's three —
  return the descriptor-derived hit rows THEMSELVES, as a bare object or array. There is no
  envelope to pin without changing the wire shape the zones and `@rask/explorer-api` already read,
  which is a frontend-plane change, not a typing one. They are named in the drain report as
  survivors rather than quietly rounded up.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from viewer.api.v1.router import router as viewer_router


#: (method, path) → the operations whose 200 body is a fixed envelope.
ENVELOPE_ROUTES = [
    ("get", "/api/atlas/status"),
    ("post", "/api/atlas/chunks/by-key"),
    ("get", "/api/doc-chunks/{doc_id}"),
    ("get", "/api/chunk-alignments/{doc_id}/{group_id}/{chunk_id}"),
]


def _schema() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(viewer_router)
    return app.openapi()


def _ok_body(doc: dict[str, Any], method: str, path: str) -> dict[str, Any]:
    return doc["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]


def test_each_envelope_route_names_a_model() -> None:
    doc = _schema()
    bare = [f"{m.upper()} {p}" for m, p in ENVELOPE_ROUTES if "$ref" not in _ok_body(doc, m, p)]
    assert not bare, (
        f"{bare} document their 200 body as an untyped mapping — the envelope is fixed, so nothing but habit makes it undeclared: {[_ok_body(doc, m, p) for m, p in ENVELOPE_ROUTES]}"
    )


def test_the_declared_envelopes_carry_their_keys() -> None:
    """A model that pins nothing would satisfy the test above and document nothing."""
    doc = _schema()
    components = doc["components"]["schemas"]
    expected = {
        "/api/atlas/status": {"projected", "rows", "space", "spaces"},
        "/api/atlas/chunks/by-key": {"rows", "key_fields"},
        "/api/doc-chunks/{doc_id}": {"doc_id", "chunks"},
        "/api/chunk-alignments/{doc_id}/{group_id}/{chunk_id}": {"alignments"},
    }
    for method, path in ENVELOPE_ROUTES:
        ref = _ok_body(doc, method, path)["$ref"].rsplit("/", 1)[-1]
        assert set(components[ref]["properties"]) >= expected[path], f"{path} → {ref} is missing {expected[path] - set(components[ref]['properties'])}"


def test_the_stale_atlas_status_model_is_not_left_beside_the_live_one() -> None:
    """`viewer.schemas.atlas` already held an `AtlasStatusResponse` that no route used and whose
    `space` was a three-value StrEnum — a corpus declaring any other projection name could not be
    described by it. Two models for one answer is how the wrong one gets picked."""
    from viewer.schemas import atlas as atlas_schemas

    assert not hasattr(atlas_schemas, "AtlasSpace"), "the dead three-value space enum is still exported beside the descriptor's own AtlasSpace"
