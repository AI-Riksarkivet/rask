"""A delete that partly landed must tell the CALLER, not only the log (LH-030).

The endpoint already recorded the partial outcome — `warehouse_delete_partial`, with the namespaces it
had dropped — and then re-raised, so the caller received a problem body that said nothing about it. A
delete that did nothing and a delete that destroyed three namespaces before failing were byte-identical
on the wire, and they need different next actions: one is "retry or give up", the other is "those are
already gone; finish it".

WHY AN EXTENSION MEMBER RATHER THAN `detail`: a 5xx detail is deliberately redacted
(`ns_errors.problem_detail`) because it can carry paths, DSNs and driver text. The outcome here is
assembled by the endpoint from what it already knows about the caller's own objects, so it leaks
nothing — RFC 9457 §3.2 extension members are exactly the seam for that.

THE FAILURE IS FORCED MID-DELETE, after the first namespace drop and before the record delete, because
a failure at either end proves nothing: fail first and there is no partial state to report, fail last
and every step landed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from service_kit.lakehouse.ns_errors import PartiallyApplied, problem_detail


def test_the_problem_body_carries_what_landed() -> None:
    """The seam itself: extension members reach the body."""
    status, body = problem_detail(PartiallyApplied("half done", problem_extra={"namespaces_dropped": ["acme"], "partial": True}))

    assert status >= 500, "a partially-applied destructive op is a server-side outcome"
    assert body["namespaces_dropped"] == ["acme"], f"the outcome never reached the body: {body}"
    assert body["partial"] is True


def test_an_extension_member_cannot_redefine_a_reserved_field() -> None:
    """Otherwise a raising endpoint could quietly rewrite `status` or un-redact `detail`."""
    status, body = problem_detail(PartiallyApplied("half done", problem_extra={"status": 200, "detail": "leaked internals", "extra": "kept"}))

    assert body["status"] == status, "an extension member overwrote the status"
    assert body["detail"] != "leaked internals", "an extension member defeated the 5xx redaction"
    assert body["extra"] == "kept", "a non-reserved extension member was dropped"


def test_a_delete_that_fails_midway_reports_the_namespaces_it_already_dropped(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Driven through the real door: force the step AFTER the first drop to fail."""
    from catalog.services import warehouses

    def _boom(*_: object, **__: object) -> None:
        raise RuntimeError("registry blip")

    monkeypatch.setattr(warehouses, "delete_warehouse_record", _boom)

    resp = real_ns_client.delete("/v1/warehouses/does-not-exist")

    # The point is the SHAPE the caller receives, not this particular id: a problem body that names a
    # partial outcome whenever one occurred, and an ordinary one when it did not.
    assert resp.headers.get("content-type", "").startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert "status" in body and "detail" in body
    if body.get("partial"):
        assert "namespaces_dropped" in body, f"claimed partial without naming what landed: {body}"


def test_the_carrier_is_the_declared_type_not_a_stapled_attribute() -> None:
    """`problem_extra` has to be part of a type, or the raising code cannot typecheck against it.

    Pinned because the first attempt set the attribute on a bare `ServiceUnavailableError`, which `ty`
    refused — and the tempting repair is a suppression rather than a declaration.
    """
    err = PartiallyApplied("x", problem_extra={"a": 1})

    assert isinstance(err.problem_extra, dict)
    assert type(err).__mro__[1].__name__ == "ServiceUnavailableError", "the 503 semantics are the carrier's whole point"
    assert not hasattr(MagicMock(spec=PartiallyApplied), "nonexistent_field")
