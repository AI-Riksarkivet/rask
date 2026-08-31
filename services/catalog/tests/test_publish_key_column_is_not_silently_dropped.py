"""The publish door's gate must be at least as strong as the one the caller asked for.

Two properties, both asserted ON THE WIRE because both are contracts an external writer (Spark, an
Argo step, a person with credentials) meets through HTTP and nothing else:

1. A ``key_column`` that names no column of the version being published is REFUSED (400). The
   `not_null` assertion is the gate's identity check, and `assert_quality` SKIPS it when the column
   is absent — so a typo used to publish with the gate's central assertion missing and a 200 that
   said `published: true` with no mention of it. A gate the caller asked for and did not get must be
   an error, never a silent downgrade.
2. Where the project has a DECLARED `GateSpec`, that record governs the key column and its required
   columns are enforced on top of the caller's. An external writer — the party trusted least —
   otherwise got a weaker gate than a mover, which resolves the same declaration for itself.

Driven through a real ``dir`` namespace and real pylance writes, like `tests/unit/test_publication.py`:
the subject is what the assertions actually do to a real dataset, and a doubled namespace would prove
only that the double was called.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import connect

from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, NamespaceDep, SettingsDep, StorageOptionsDep, get_lineage_emitter
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints import publication as door
from catalog.services.dataplane import create_table
from catalog.services.publication import published_version
from service_kit.lakehouse import gate_specs
from service_kit.lakehouse.gate_specs import GateSpec
from service_kit.lakehouse.ns_errors import install_problem_handlers


lance = pytest.importorskip("lance")

_SUB = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"

#: Root-level for the same reason `tests/unit/test_publication.py` states: a child namespace needs its
#: own `__manifest` dataset and publication is a per-TABLE concern.
TABLE_ID = ["pages"]

SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("payload", pa.string())])

#: The project the stubbed lineage binding resolves `pages` to — the key a declared gate is stored under.
PROJECT = "acme"


def _ipc(ids: list[int]) -> bytes:
    table = pa.table({"id": pa.array(ids, pa.int64()), "payload": pa.array([f"p{i}" for i in range(len(ids))])}, schema=SCHEMA)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def registry_root(tmp_path: Path) -> str:
    """Where a declared `GateSpec` lives — the catalog's own control root."""
    root = tmp_path / "control"
    root.mkdir()
    return str(root)


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace, a runtime-only type
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    create_table(namespace, {}, TABLE_ID, _ipc([1, 2, 3]), mode="create")
    return namespace


@pytest.fixture
def app(ns, registry_root: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:  # noqa: ANN001
    """The publication router alone, with the estate's problem handlers installed.

    The FGA gate is not the subject here (its own suites own it) and the control emit is a
    best-effort side channel, so both are stubbed; everything the gate reads is real.
    """
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)

    settings = SimpleNamespace(delimiter="$", registry_root=registry_root, storage_options=lambda: {})
    application.dependency_overrides[SettingsDep.__metadata__[0].dependency] = lambda: settings
    application.dependency_overrides[NamespaceDep.__metadata__[0].dependency] = lambda: ns
    application.dependency_overrides[StorageOptionsDep.__metadata__[0].dependency] = lambda: {}
    application.dependency_overrides[CurrentToken.__metadata__[0].dependency] = lambda: SimpleNamespace(sub=_SUB)
    application.dependency_overrides[FgaClientDep.__metadata__[0].dependency] = lambda: object()
    application.dependency_overrides[ControlEmitterDep.__metadata__[0].dependency] = lambda: None
    application.dependency_overrides[get_lineage_emitter] = lambda: SimpleNamespace(project_for=_project_for)

    async def _noop_emit(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(door, "emit_control", _noop_emit)
    yield application


async def _project_for(_top_ns: str) -> str:
    return PROJECT


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _publish(client: TestClient, **body: Any) -> Any:  # noqa: ANN401 — httpx.Response
    return client.post("/v1/table/pages/publish", json={"version": 1, **body})


def _assertion(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((a for a in payload["assertions"] if a["assertion"] == name), None)


# --- 1 THE REFUSAL: a key column the data does not carry ------------------------------------------


def test_a_key_column_the_table_does_not_have_is_REFUSED(client: TestClient) -> None:
    """The headline defect: a typo published with the identity assertion silently missing."""
    response = _publish(client, key_column="nope")

    assert response.status_code == 400, response.text
    body = response.json()
    assert "nope" in body["detail"], body
    assert body["code"], "a catalog 400 carries the spec's numeric code, not a bare about:blank body"


def test_the_REFUSED_publish_moves_nothing(client: TestClient, ns) -> None:  # noqa: ANN001
    """A refusal is fail-closed: the pointer stays where it was, so no consumer sees the version."""
    _publish(client, key_column="nope")

    assert published_version(ns, {}, TABLE_ID) is None


def test_the_same_refusal_applies_to_the_gate_only_QUESTION(client: TestClient) -> None:
    """`gate_only` must answer exactly what `publish` would do — a verdict that differed from the act
    is worse than no verdict, because a caller trusts it."""
    response = _publish(client, key_column="nope", gate_only=True)

    assert response.status_code == 400, response.text


def test_a_REAL_key_column_still_publishes(client: TestClient) -> None:
    """The guard against over-refusal: the ordinary publish is untouched."""
    response = _publish(client, key_column="id")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["published"] is True
    assert _assertion(payload, "not_null") == {"assertion": "not_null", "success": True, "column": "id"}


# --- 2 PARITY: the declared gate governs this door too --------------------------------------------


def test_the_DECLARED_key_column_governs_over_the_callers(client: TestClient, registry_root: str) -> None:
    """The external writer is the party trusted LEAST, so it must not be the one that picks the gate.

    The caller names a column that exists — this is not the refusal above — and the declaration still
    wins, because a project's declared gate is policy and a request field is a request.
    """
    gate_specs.put_spec(registry_root, {}, GateSpec(project=PROJECT, key_column="id"))

    payload = _publish(client, key_column="payload").json()

    assert payload["published"] is True
    assert _assertion(payload, "not_null") == {"assertion": "not_null", "success": True, "column": "id"}


def test_the_DECLARED_required_columns_are_enforced_on_a_caller_that_named_none(client: TestClient, registry_root: str) -> None:
    """A caller cannot drop a declared column dependency by simply not mentioning it."""
    gate_specs.put_spec(registry_root, {}, GateSpec(project=PROJECT, key_column="id", required_columns=["payload"]))

    payload = _publish(client, key_column="id").json()

    declared = [a for a in payload["assertions"] if a["assertion"] == "column_declared"]
    assert declared == [{"assertion": "column_declared", "success": True, "column": "payload"}], payload["assertions"]


def test_a_DECLARED_column_the_table_lost_BLOCKS_the_publish(client: TestClient, registry_root: str, ns) -> None:  # noqa: ANN001
    """The point of enforcing it: the gate refuses the promotion, not merely reports it."""
    gate_specs.put_spec(registry_root, {}, GateSpec(project=PROJECT, key_column="id", required_columns=["thumbnail"]))

    payload = _publish(client, key_column="id").json()

    assert payload["published"] is False
    assert "column_declared" in (payload["reason"] or "")
    assert published_version(ns, {}, TABLE_ID) is None


def test_the_RESULT_NAMES_WHICH_GATE_RAN(client: TestClient, registry_root: str) -> None:
    """Attribution, the rule the catalog's own policy ruling states: a surface showing an effective
    policy must say which record won. Two sources with one shape is how nobody can tell what governed
    their data — and here the two disagree about which column the identity check ran on."""
    assert _publish(client, key_column="id").json()["gate_source"] == "request"

    gate_specs.put_spec(registry_root, {}, GateSpec(project=PROJECT, key_column="id"))

    assert _publish(client, key_column="id").json()["gate_source"] == "declared"


def test_a_DECLARED_key_column_the_table_lacks_is_REFUSED_and_NAMES_the_declaration(client: TestClient, registry_root: str) -> None:
    """Fail-closed in the other direction, and the message must send the operator to the right lever:
    the column is not in the request they sent, so an error naming only the column reads as a bug."""
    gate_specs.put_spec(registry_root, {}, GateSpec(project=PROJECT, key_column="absent"))

    response = _publish(client, key_column="id")

    assert response.status_code == 400, response.text
    assert "absent" in response.json()["detail"]
    assert PROJECT in response.json()["detail"], "a refusal caused by a declaration must name the project that declared it"
