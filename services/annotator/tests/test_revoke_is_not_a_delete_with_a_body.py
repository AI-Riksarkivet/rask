"""Member revoke must not depend on a DELETE request body (ANN-20).

RFC 9110 gives a DELETE body no defined semantics, and real intermediaries (proxies, some HTTP
clients) strip or refuse it — a revoke that 422s only behind certain infra is the kind of failure
nobody reproduces locally. The revoke's two fields ride as query params instead, where every hop
preserves them.
"""

from __future__ import annotations

from annotator.api.v1.endpoints import members
from fastapi.routing import APIRoute


def _delete_route() -> APIRoute:
    deletes = [r for r in members.router.routes if isinstance(r, APIRoute) and "DELETE" in (r.methods or set())]
    assert len(deletes) == 1, f"expected exactly one DELETE route on the members router, found {len(deletes)}"
    return deletes[0]


def test_revoke_takes_no_request_body() -> None:
    route = _delete_route()
    assert route.body_field is None, (
        f"the revoke DELETE declares a request body ({route.body_field!r}) — "
        "DELETE bodies are stripped by some intermediaries; the fields belong in query params"
    )


def test_revoke_reads_user_and_relation_from_query_params() -> None:
    query_params = {p.name for p in _delete_route().dependant.query_params}
    assert {"user", "relation"} <= query_params, f"revoke must bind user+relation as query params; found {sorted(query_params)}"
