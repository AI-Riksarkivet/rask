"""Shared fixtures for the medallion suite.

ONE FIXTURE, and it exists because a governance call was added to a path many of these tests drive
for other reasons. `_run_compute` now asks the catalog to authorize the stage's WRITE before anything
is dispatched (`catalog_register.authorize_stage_write`), which is a real HTTP request. Tests that
stub `ensure_stage_output` to drive the write path — and there are nine — would otherwise fail on a
connection error while testing something else entirely.

DEFAULTED, NOT DISABLED. The stub answers what the live catalog answers today (`server_mediated`,
measured against `bind86-gold$catalog` with the mover's own credential), and it RECORDS its calls, so
a test that cares about the authorization can assert on it rather than re-stub. The gate that proves
the call happens at all lives in `tests/unit/test_the_cascade_authorizes_its_own_writes.py`, where it
is checked against the parsed source rather than against a double — a double can only show that a
stub was called, never that production calls it.
"""

from __future__ import annotations

from typing import Any

import pytest

from medallion.services import catalog_register


@pytest.fixture(autouse=True)
def stub_stage_write_authorization(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Answer the write-authorization call in-process; return the list of calls made."""
    calls: list[dict[str, Any]] = []

    def _authorize(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "server_mediated"

    monkeypatch.setattr(catalog_register, "authorize_stage_write", _authorize)
    return calls
