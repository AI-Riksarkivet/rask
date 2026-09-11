"""The register door stamps the location it was SENT, which is relative and resolves nowhere.

`register_table` echoes the caller's own relative path back: measured 2026-09-11 against a real `dir`
namespace, registering `location="t.lance"` answers `response.location == 't.lance'`, while
`describe_table` on the same table answers the absolute `'/tmp/<root>/t.lance'`. The door emits
`source_uri=response.location` onto the CREATED edge and `extra={"location": ...}` onto the control
event, so both carry the relative form.

WHAT THAT COSTS IS A PERMANENT FALSE ALARM. The reconcile sweep opens that `source_uri`; a relative path
opens as nothing, and `read_storage_version` answers `None`, which classifies MISSING_ON_STORAGE and is
reported as storage loss — measured by calling it: both `'medallion/bronze'` and `'t.lance'` answer
`None`. So a table that registered successfully and holds data is reported as destroyed on every tick,
forever, and no re-run clears it because the graph keeps the URI it was given.

IT IS ALSO WHAT THE PUBLISH EVENT CARRIES. `medallion.services.catalog_register` logs
`location: medallion/bronze` on the live estate — the relative form — and that value rides
`extra["location"]` to consumers that resolve an upstream by it.

THE ABSOLUTE FORM IS ALREADY AVAILABLE and costs one metadata call: `describe_table` resolves the
location against whatever root the table actually belongs to, including a warehouse-bound one, which is
the part a caller-side join against a configured root would get wrong for exactly the tables that
matter.

FAILING TO RESOLVE MUST NOT FAIL THE REGISTER. The door's own comment already sets that rule — "a
reopen failure must never fail an already-committed register" — so an unresolvable describe leaves
today's value rather than raising. That is a narrower degradation than it looks: `describe_table` is a
metadata read, not a dataset open, so it does not reintroduce the reopen the comment refuses.
"""

from __future__ import annotations

from typing import Any, cast

from catalog.api.v1.endpoints import tables as tables_endpoint


class _Namespace:
    """A backend whose describe knows the absolute location, as the real one does."""

    def __init__(self, location: str | None) -> None:
        self._location = location
        self.described = 0


def _describe(location: str | None) -> Any:
    class _Response:
        def __init__(self) -> None:
            self.location = location

    return _Response()


def test_the_absolute_location_replaces_the_echoed_relative_one(monkeypatch: Any) -> None:
    """THE GATE. A relative `source_uri` is a dataset the sweep can never find."""
    monkeypatch.setattr(tables_endpoint.native, "call", lambda _ns, _op, _req: _describe("s3://wh/9f_ns$t"))

    resolved = tables_endpoint.absolute_table_location(cast(Any, _Namespace("s3://wh/9f_ns$t")), ["ns", "t"], "t.lance")

    assert resolved == "s3://wh/9f_ns$t", "the marker must carry a URI that opens, not the caller's relative path"


def test_an_unresolvable_describe_keeps_the_registered_value(monkeypatch: Any) -> None:
    """The register is already committed; a metadata read that fails must not undo or fail it.

    Degrading to today's value is the honest answer — the edge is no worse than before, and raising
    here would turn a successful registration into a 500 the caller cannot retry into a better state.
    """

    def _boom(_ns: object, _op: str, _req: object) -> object:
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr(tables_endpoint.native, "call", _boom)

    assert tables_endpoint.absolute_table_location(cast(Any, _Namespace(None)), ["ns", "t"], "t.lance") == "t.lance"


def test_a_describe_without_a_location_keeps_the_registered_value(monkeypatch: Any) -> None:
    """An empty answer is not a better answer — `None` must not overwrite what the register returned."""
    monkeypatch.setattr(tables_endpoint.native, "call", lambda _ns, _op, _req: _describe(None))

    assert tables_endpoint.absolute_table_location(cast(Any, _Namespace(None)), ["ns", "t"], "t.lance") == "t.lance"


def test_the_register_door_emits_the_resolved_location() -> None:
    """Pinned by source: a helper nothing calls is the control that cannot fire, one more time."""
    import inspect

    body = inspect.getsource(tables_endpoint.register_table)

    # The NAME, not `name(` — the resolver is handed to `run_in_threadpool` as a callable, so the call
    # parenthesis never appears at the site. Asserting the call syntax failed against correct code.
    assert "absolute_table_location" in body, "the door must resolve before it emits"
    resolved_at = body.index("absolute_table_location")
    assert resolved_at < body.index("emit_write_event("), "the CREATED edge must carry the resolved URI"
    assert resolved_at < body.index("emit_control("), "the control event's `location` extra must carry it too"
