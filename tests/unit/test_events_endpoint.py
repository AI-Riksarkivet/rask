"""Unit tests for the control-plane poll endpoint (``GET /v1/events``, endpoints/events.py).

Drives the handler directly (no TestClient) with a fake buffer + settings, mirroring the repo's direct-call
style. Pins the two design-review fixes: #10 (``event_stream_opened`` is audited ONLY on a meaningful poll —
events delivered or a reset — never an empty tick, so a 5s-polling console can't flood the audit trail) and
#12 (the gate is an estate-admin ``can_observe_events`` check on the fixed root object, not a per-project
one). The estate-scope authorization itself is proven at the model tier in ``model.fga.yaml``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from catalog.api.v1.endpoints import events as ep
from catalog.core.control_buffer import ControlEventBuffer
from common.control_events import CatalogControlEvent


def _evt(object_id: str = "table:db1$t") -> CatalogControlEvent:
    return CatalogControlEvent(action="grant_added", object_type="grant", object_id=object_id, actor="user:alice")


def _settings(*, fga_enabled: bool = False) -> Any:
    # fga_enabled=False makes require_relation a genuine no-op (its own guard), so these tests exercise the
    # buffer + audit logic without a live OpenFGA; the gate's args are asserted separately below.
    return SimpleNamespace(fga_enabled=fga_enabled, fga_root_object="warehouse:lance_catalog")


def _request(buffer: ControlEventBuffer | None) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(control_buffer=buffer)))


class _AuditRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, action: str, _outcome: str, **fields: Any) -> None:
        self.calls.append((action, fields))


def _call(req: Any, settings: Any, *, since: int, token: Any = None) -> ep.EventsResponse:
    return asyncio.run(ep.poll_control_events(request=req, settings=settings, token=token, client=None, since=since))


# ── #10: audit only a MEANINGFUL poll ────────────────────────────────────────────────────────────────


def test_empty_poll_writes_no_audit(monkeypatch: Any) -> None:
    rec = _AuditRecorder()
    monkeypatch.setattr(ep, "audit", rec)
    buf = ControlEventBuffer(8)
    buf.append(_evt())  # head=1
    # A caught-up poll (since == head) delivers nothing new → NOT a meaningful poll, no audit written.
    res = _call(_request(buf), _settings(), since=1)
    assert res.events == [] and res.cursor == 1 and res.reset is False
    assert rec.calls == []  # the 5s steady-state empty tick must NOT touch the audit trail


def test_delivered_events_write_one_audit(monkeypatch: Any) -> None:
    rec = _AuditRecorder()
    monkeypatch.setattr(ep, "audit", rec)
    buf = ControlEventBuffer(8)
    buf.append(_evt("a"))  # cursor 1
    buf.append(_evt("b"))  # cursor 2
    res = _call(_request(buf), _settings(), since=1, token=SimpleNamespace(sub="root_admin"))
    assert [e.object_id for e in res.events] == ["b"] and res.cursor == 2
    assert len(rec.calls) == 1
    action, fields = rec.calls[0]
    assert action == "event_stream_opened"
    assert fields["resource"] == "warehouse:lance_catalog"  # #12: audited against the root object
    assert fields["subject"] == "root_admin" and fields["delivered"] == 1 and fields["reset"] is False


def test_reset_writes_audit(monkeypatch: Any) -> None:
    rec = _AuditRecorder()
    monkeypatch.setattr(ep, "audit", rec)
    buf = ControlEventBuffer(2)  # tiny ring → overflow
    for i in range(5):
        buf.append(_evt(str(i)))
    # A client stuck at cursor 1 fell off the end → reset=True is a meaningful poll even though it also
    # carries events; audit fires so the operator sees the gap-recovery.
    res = _call(_request(buf), _settings(), since=1)
    assert res.reset is True
    assert len(rec.calls) == 1 and rec.calls[0][1]["reset"] is True


def test_missing_buffer_is_safe(monkeypatch: Any) -> None:
    rec = _AuditRecorder()
    monkeypatch.setattr(ep, "audit", rec)
    # A test app / lifespan that never built the buffer → empty baseline, no audit, no crash.
    res = _call(_request(None), _settings(), since=0)
    assert res.events == [] and res.cursor == 0 and res.reset is False and rec.calls == []


# ── #12: the gate is an estate-admin can_observe_events check on the root object ──────────────────────


def test_gate_uses_can_observe_events_on_root_object(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    async def _fake_require(_client: Any, _settings: Any, _token: Any, *, relation: str, obj: str) -> None:
        seen["relation"] = relation
        seen["obj"] = obj

    monkeypatch.setattr(ep.fga_deps, "require_relation", _fake_require)
    monkeypatch.setattr(ep, "audit", _AuditRecorder())
    _call(_request(ControlEventBuffer(4)), _settings(fga_enabled=True), since=0)
    # NOT a per-project can_administer — the estate-wide feed is gated on the fixed root object so authz
    # scope == data scope (the #12 fix). cast keeps the fake's kwargs untyped-friendly.
    assert seen == cast(Any, {"relation": "can_observe_events", "obj": "warehouse:lance_catalog"})
