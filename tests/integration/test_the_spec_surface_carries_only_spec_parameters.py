"""A spec-prefixed route may take only query parameters the Lance Namespace spec defines.

[[LH-021]]'s SECOND closing clause — "a spec client observes only spec-shaped responses there". Its
sibling `test_the_spec_surface_carries_only_spec_operations.py` closed the ROUTE half and is
structurally blind to this one: a route can be a perfectly legal spec operation and still advertise
a parameter no spec client has vocabulary for.

THE SPEC SIDE IS DERIVED, NOT LISTED. It is the union of every request model's fields in the pinned
`lance_namespace_urllib3_client`, plus `delimiter` — the one documented query parameter that is
transport rather than a model field (`lance_docs/namespace.md:1221-1224`, which also documents
`page_token` and `limit`; those two ARE model fields and arrive by the union). Deriving from the
client rather than naming parameters means a spec version bump moves this gate with it, and it is
why the row's own headline example turned out to be wrong: `branch` is a field on the upstream
`DescribeTableRequest`, so honouring it on a spec route is conformance, not dialect.

WHAT A FAILURE MEANS. rask extensions are fine on the WIRE — they are how the estate adds capability
to a spec operation it cannot move off `/v1`. What must not happen is the document advertising them,
because a spec client discovering the surface then meets a parameter its vocabulary cannot explain.
The fix is `Query(include_in_schema=False)`, which changes the document and not the wire.

Headers are deliberately NOT checked here: that class has typed consumers and is an open decision on
the row, and a gate that fails on an undecided question is one people learn to skip.
"""

from __future__ import annotations

import inspect
from typing import Any, cast

import lance_namespace_urllib3_client.models as spec_models
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


#: The four prefixes the spec owns.
_SPEC_PREFIXES = ("/v1/namespace", "/v1/table", "/v1/materialized_view", "/v1/transaction")
#: Documented as a query parameter but carried by no request model — it encodes the id, not the request.
_TRANSPORT_PARAMS = frozenset({"delimiter"})


def _spec_vocabulary() -> frozenset[str]:
    """Every field name any spec request/response model declares."""
    fields: set[str] = set()
    for name in dir(spec_models):
        candidate = getattr(spec_models, name)
        if inspect.isclass(candidate) and hasattr(candidate, "model_fields"):
            fields |= set(candidate.model_fields)
    return frozenset(fields) | _TRANSPORT_PARAMS


def _non_spec_query_params(client: TestClient) -> dict[tuple[str, str], list[str]]:
    """(METHOD, path) -> the query parameters the document advertises that the spec does not define."""
    vocabulary = _spec_vocabulary()
    paths: dict[str, Any] = cast(FastAPI, client.app).openapi().get("paths", {})
    offenders: dict[tuple[str, str], list[str]] = {}
    for path, operations in paths.items():
        if not path.startswith(_SPEC_PREFIXES):
            continue
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                continue
            extra = sorted(p["name"] for p in operation.get("parameters", []) if p.get("in") == "query" and p["name"] not in vocabulary)
            if extra:
                offenders[(method.upper(), path)] = extra
    return offenders


def test_no_spec_route_advertises_a_parameter_the_spec_does_not_define(client: TestClient) -> None:
    offenders = _non_spec_query_params(client)

    assert not offenders, (
        "these spec routes advertise rask-only query parameters, so a spec client reading the document "
        "meets vocabulary it cannot explain — hide them with `Query(include_in_schema=False)`, which "
        "leaves the wire untouched:\n  " + "\n  ".join(f"{method} {path}: {names}" for (method, path), names in sorted(offenders.items()))
    )


def test_the_spec_vocabulary_is_actually_populated() -> None:
    """Without this, an import that silently yielded nothing would make the gate above vacuous."""
    vocabulary = _spec_vocabulary()

    assert len(vocabulary) > 100, f"only {len(vocabulary)} spec field names were derived — the walk is broken, not clean"
    assert {"branch", "page_token", "limit", "delimiter", "mode"} <= vocabulary


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/v1/table/nsx$tx/drop", "force=notabool"),
        ("/v1/table/nsx$tx/drop", "purge=notabool"),
        ("/v1/namespace/nsx/drop", "force=notabool"),
        ("/v1/table/nsx$tx/rename", "force=notabool"),
        ("/v1/table/nsx$tx/deregister", "force=notabool"),
    ],
)
def test_a_hidden_parameter_is_still_bound_on_the_wire(client: TestClient, path: str, query: str) -> None:
    """Hiding is a DOCUMENT change only — an unbound parameter would be ignored, not rejected.

    A VALIDATION refusal is the proof: FastAPI silently drops a query parameter no handler declares, so
    a malformed value can only be refused by a parameter that is still bound. Without this,
    `include_in_schema=False` could be swapped for deleting the parameter and every other test in this
    file would still pass.

    Keyed on the problem TYPE rather than the status, because this estate maps the spec's InvalidInput
    onto 400 — so a bare `== 422` would assert FastAPI's default instead of what the door answers, and
    a 400 raised for some unrelated reason would satisfy a bare `== 400`.
    """
    response = client.post(f"{path}?{query}")

    assert response.json().get("type", "").endswith("/validation"), (
        f"{path}?{query} was not refused as a validation error (got {response.status_code} "
        f"{response.json()}) — the parameter is no longer bound, so hiding it from the document has "
        "become removing it from the wire"
    )
