"""The FGA gate must authorize the table the handler will ACTUALLY read.

Two commits met badly here. `feat(descriptor): a corpus can declare SEVERAL searchable tables`
(c60773f7, 2026-08-04) turned `declared.search` into a list and made `?table=` select an entry by
name, with `search_named(None)` for the default and `None` — never a fallback — for a name the
corpus does not declare: "a search that quietly answered from a different table than the one asked
for is a wrong answer, not a lenient one". `fix(search): the last unguarded door on the
/api/explorer edge` (3593381c, 2026-08-28) then built the gate against the shape that commit had
already superseded, reading `declared.search` — the FIRST entry — while every handler resolves rows
from `?table=`.

So the gate could authorize table A and the handler read table B. It is latent only because no
descriptor on disk declares two searchable tables yet; the descriptor contract has allowed it for
three weeks, and the failure it produces is a silent read of a table the caller was never checked
against.

Three properties, all driven over the real routes because the wiring is half the defect — a gate
that resolves the right table from a parameter the route never hands it is still broken:

* the selected table is what gets checked (allowed AND refused, on the same corpus);
* a grant on the default does not unlock a second table;
* a `?table=` the corpus does not declare FAILS CLOSED, rather than being authorized against the
  default and then 400'd by `resolve_target` afterwards.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import service_kit.media.state as state_mod
from search.api import security
from search.api.dependencies import StateDep
from search.api.v1 import router as router_module
from search.core.config import SearchSettings, get_search_settings
from service_kit.exceptions import register_handlers
from service_kit.lancekit.descriptor import Declared
from service_kit.media.authz import corpus_object


DATASET = "vasa"

#: A corpus that declares TWO searchable tables — the shape c60773f7 made legal. `default` is the
#: first, so it is what `declared.search` returns and what the gate checked regardless of `?table=`.
DECLARED = Declared.model_validate(
    {
        "identity": {"key_fields": ["doc_id"], "doc_key": "doc_id"},
        "searches": [
            {"name": "default", "row_table": "default_rows", "filterable": ["doc_id"]},
            {"name": "frames", "row_table": "frames_rows", "filterable": ["doc_id"]},
        ],
    }
)

SETTINGS = SearchSettings.model_validate(
    {"LANCE_FGA_ENABLED": True, "LANCE_OIDC_ENABLED": True, "LANCE_OIDC_ISSUER": "https://i.test", "LANCE_OIDC_AUDIENCE": "rask"}
)

DEFAULT_OBJECT = corpus_object(SETTINGS, DATASET, "default_rows")
FRAMES_OBJECT = corpus_object(SETTINGS, DATASET, "frames_rows")


class _Handle:
    """The registry entry both the gate and the handler resolve — only `id` and the descriptor's
    declared half are read on these paths."""

    id = DATASET
    descriptor = type("D", (), {"id": DATASET, "declared": DECLARED})()


@pytest.fixture
def checked() -> list[str]:
    return []


def _client(monkeypatch: pytest.MonkeyPatch, *, granted: set[str], checked: list[str]) -> TestClient:
    """The search app with the corpus resolved to `_Handle` and a checker that grants `granted`.

    Both resolution sites are patched: the gate imports `dataset_handle` from
    `service_kit.media.state` inside `may_search`, the handler holds it in the router's namespace.
    """
    monkeypatch.setattr(state_mod, "dataset_handle", lambda *_a, **_k: _Handle())
    monkeypatch.setattr(router_module, "dataset_handle", lambda *_a, **_k: _Handle())

    async def check(*, user: str, relation: str, obj: str) -> bool:
        checked.append(obj)
        return obj in granted

    app = FastAPI()
    app.include_router(router_module.router)
    register_handlers(app)
    app.dependency_overrides[get_search_settings] = lambda: SETTINGS
    app.dependency_overrides[StateDep.__metadata__[0].dependency] = lambda: object()
    app.dependency_overrides[security._deps.current_subject] = lambda: "alice"
    app.dependency_overrides[security._deps.get_checker] = lambda: check
    return TestClient(app, raise_server_exceptions=False)


def _get(client: TestClient, **params: Any) -> Any:
    # No `q`: the handler short-circuits to `[]` once past the gate, so the STATUS is purely the
    # gate's answer — 403 or 200 — with no Lance or embedder anywhere in the path.
    return client.get("/api/search", params={"dataset": DATASET, **params})


def test_a_grant_on_the_SELECTED_table_allows_it(monkeypatch: pytest.MonkeyPatch, checked: list[str]) -> None:
    """The allowed case. A caller entitled to `frames` and nothing else may search `?table=frames` —
    the gate checked `default_rows`, which they do not hold, and refused a search they may make."""
    client = _client(monkeypatch, granted={FRAMES_OBJECT}, checked=checked)

    r = _get(client, table="frames")

    assert r.status_code == 200, f"a caller granted the table they asked for was refused it: {r.status_code} {r.text}"
    assert checked == [FRAMES_OBJECT], f"the gate authorized {checked} rather than the table the handler reads"


def test_a_grant_on_the_DEFAULT_table_does_not_unlock_a_SECOND_one(monkeypatch: pytest.MonkeyPatch, checked: list[str]) -> None:
    """The refused case, and the security half: `declared.search` is `searches[0]`, so a caller
    holding only the default was authorized against it and then served rows from `frames`."""
    client = _client(monkeypatch, granted={DEFAULT_OBJECT}, checked=checked)

    r = _get(client, table="frames")

    assert r.status_code == 403, f"a grant on the default table authorized a search of another one: {r.status_code}"
    assert checked == [FRAMES_OBJECT], f"the gate authorized {checked} rather than the table the handler reads"


def test_the_DEFAULT_table_is_still_gated_on_its_own_object(monkeypatch: pytest.MonkeyPatch, checked: list[str]) -> None:
    """No `?table=` still means the corpus's first declared table — the case every descriptor on
    disk exercises today, which must not change."""
    client = _client(monkeypatch, granted={DEFAULT_OBJECT}, checked=checked)

    assert _get(client).status_code == 200
    assert checked == [DEFAULT_OBJECT]


def test_an_UNDECLARED_table_FAILS_CLOSED(monkeypatch: pytest.MonkeyPatch, checked: list[str]) -> None:
    """A `?table=` this corpus does not declare names no FGA object, so it is DENIED rather than
    checked against an invented — or borrowed — identifier. `datasets.py`'s rule, and the reason the
    gate already denies a corpus that declares no search block at all."""
    client = _client(monkeypatch, granted={DEFAULT_OBJECT, FRAMES_OBJECT}, checked=checked)

    r = _get(client, table="ghost")

    assert r.status_code == 403, f"an undeclared table was authorized against another table's grant: {r.status_code}"
    assert checked == [], f"the gate checked {checked} for a table the corpus does not declare"


def test_the_SIMILAR_route_gates_the_table_it_was_asked_for(monkeypatch: pytest.MonkeyPatch, checked: list[str]) -> None:
    """`/search/similar` calls `require_search` directly rather than through the fan-out dependency,
    so it is a second wiring that can drift. It reads rows from `?table=` exactly as `/search` does."""
    client = _client(monkeypatch, granted={DEFAULT_OBJECT}, checked=checked)

    r = client.get("/api/search/similar", params={"key": "d1", "dataset": DATASET, "table": "frames"})

    assert r.status_code == 403, f"the seed row was read from a table this caller was never checked against: {r.status_code}"
    assert checked == [FRAMES_OBJECT], f"the gate authorized {checked} rather than the table the seed is read from"
