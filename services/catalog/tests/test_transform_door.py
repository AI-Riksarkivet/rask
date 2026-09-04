"""The lane-declaration door: admin-gated, durable, and 422 — naming the key — for an unknown lane.

Driven through a real ASGI client rather than by calling the handlers, because two of the three
properties under test only exist ON THE WIRE. A handler that raises `RequestValidationError` proves
nothing about the status code or the body shape an operator actually receives; the installed problem
handler is what turns it into a 422 whose `errors[].field` names `body.lane`, and that handler is
part of the contract being asserted.

The FGA gate is exercised as three distinct outcomes, not one: allowed, denied, and OUTAGE. The third
is the one worth pinning — an authz layer that is down must fail closed (503), never fall through to
a permissive default, on a door whose records name programs that execute on the shared cluster.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import PermissionDeniedError, ServiceUnavailableError

from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints import transforms
from service_kit.lakehouse import task_registry, transform_specs
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.lakehouse.task_registry import TaskRegistration


_SUB = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"

VALID: dict[str, Any] = {
    "name": "dummy",
    "from_id": "bronze$events",
    "to_id": "silver$dummy",
    "task": "dummy-lane",
    "params": {"batch_size": "64"},
    "code_version": "main-abc1234",
}


@pytest.fixture
def control_root(tmp_path: Path) -> str:
    return str(tmp_path)


@pytest.fixture
def app(control_root: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    """The transform router alone, with the estate's problem handlers installed.

    `require_relation` is patched per test rather than stood up against a real OpenFGA: the gate's
    own semantics are pinned in the fga_deps suite, and what this module asserts is that THIS door
    calls it, on the right object, before touching storage.
    """
    # THE REGISTRY THE DOOR CONSULTS. Seeded here rather than mocked because the door's refusal is a
    # real lookup against the control root, and a mock would let the two spellings of "registered"
    # drift — the executor plane writes the same record this reads.
    task_registry.put_task(
        control_root, {}, TaskRegistration(task="dummy-lane", engine="ray", command="python /home/ray/jobs/ray_dummy_job.py", cardinalities=["1:1"])
    )
    task_registry.put_task(control_root, {}, TaskRegistration(task="stage-transform", engine="ray", command="python /home/ray/jobs/ray_stage_job.py"))

    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(transforms.router)

    settings = SimpleNamespace(registry_root=control_root, storage_options=lambda: {}, delimiter="$")
    application.dependency_overrides[SettingsDep.__metadata__[0].dependency] = lambda: settings
    application.dependency_overrides[CurrentToken.__metadata__[0].dependency] = lambda: SimpleNamespace(sub=_SUB)
    application.dependency_overrides[FgaClientDep.__metadata__[0].dependency] = lambda: object()
    application.dependency_overrides[ControlEmitterDep.__metadata__[0].dependency] = lambda: None

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(transforms.fga_deps, "require_relation", _allow)

    # The control emit is a best-effort side channel; the audit lane has its own suite.
    async def _noop_emit(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(transforms, "emit_control", _noop_emit)
    yield application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# --- 1 DECLARED: the record exists, and it survives the process that wrote it ----------------------


def test_declaring_a_lane_persists_it_and_answers_with_the_stored_record(client: TestClient, control_root: str) -> None:
    response = client.post("/v1/project/acme/transform/set", json=VALID)

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "dummy"
    assert response.json()["project"] == "acme", "the project must come from the gated path, not the body"

    # Read back through the registry directly — no request, no app, no shared memory. This is the
    # same call a mover pod makes, and it is what "survives a pod restart" reduces to.
    stored = transform_specs.get_spec(control_root, {}, "acme", "dummy")
    assert stored is not None and stored.task == VALID["task"]


def test_a_body_supplied_project_is_REFUSED_rather_than_honoured(client: TestClient) -> None:
    """Cross-tenant declaration: the gate runs on the PATH project, so a body project that differed
    would be written under a tenant the caller was never checked against."""
    response = client.post("/v1/project/acme/transform/set", json={**VALID, "project": "globex"})

    assert response.status_code == 422, response.text


def test_declaring_is_idempotent(client: TestClient, control_root: str) -> None:
    client.post("/v1/project/acme/transform/set", json=VALID)
    client.post("/v1/project/acme/transform/set", json={**VALID, "code_version": "main-def5678"})

    listed = client.get("/v1/projects/acme/transforms").json()["transforms"]
    assert len(listed) == 1
    assert listed[0]["code_version"] == "main-def5678"


# --- the 422 that names the key -------------------------------------------------------------------


def test_an_unknown_transform_is_422_NAMING_the_key(client: TestClient) -> None:
    """The headline property of condition 1.

    Not 404: the URL is right and the key inside it is not, which is the same class of fault as a bad
    enum value. The body must name the offending field, or an operator reading the error learns only
    that something was wrong.
    """
    client.post("/v1/project/acme/transform/set", json=VALID)

    response = client.post("/v1/project/acme/transform/describe", json={"name": "nosuchlane"})

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["status"] == 422
    fields = [e["field"] for e in body["errors"]]
    assert "body.name" in fields, f"the 422 must name the transform field; got {fields}"
    assert "nosuchlane" in body["errors"][0]["message"], "the message must name the undeclared key"


def test_an_UNREGISTERED_task_is_422_at_the_DOOR(client: TestClient) -> None:
    """A task no executor registered cannot be declared, so it can never be submitted.

    The refusal is the whole reason the registry is consulted HERE. A submit-time check has to be
    remembered by every submit path, and the one that forgets fails deep on the cluster with an
    error naming an image rather than the key nobody registered.
    """
    response = client.post("/v1/project/acme/transform/set", json={**VALID, "task": "nobody-registered-this"})

    assert response.status_code == 422, response.text
    body = response.json()
    assert "body.task" in [e["field"] for e in body["errors"]], f"the 422 must name the task field; got {body['errors']}"
    assert "nobody-registered-this" in response.text, "the message must name the unregistered task"


def test_a_cardinality_the_TASK_cannot_honour_is_422_at_the_DOOR(client: TestClient) -> None:
    """The check a path-shaped allowlist could never make.

    `dummy-lane` merge-inserts one row per input. Declaring it 1:N passes the job's own count check —
    the job enforces the shape it was TOLD — so the mismatch is a silent data defect unless it is
    refused where the declaration and the registration are both in scope.
    """
    response = client.post("/v1/project/acme/transform/set", json={**VALID, "cardinality": "1:N"})

    assert response.status_code == 422, response.text
    assert "body.cardinality" in [e["field"] for e in response.json()["errors"]]
    assert "dummy-lane" in response.text, "the message must name the task that cannot honour it"


def test_an_unsafe_lane_key_is_422(client: TestClient) -> None:
    response = client.post("/v1/project/acme/transform/set", json={**VALID, "name": "../escape"})

    assert response.status_code == 422, response.text


# --- the gate ------------------------------------------------------------------------------------


def test_a_non_admin_is_REFUSED_and_nothing_is_written(client: TestClient, control_root: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _deny(*_a: Any, **_k: Any) -> None:
        raise PermissionDeniedError("not an admin")

    monkeypatch.setattr(transforms.fga_deps, "require_relation", _deny)

    response = client.post("/v1/project/acme/transform/set", json=VALID)

    assert response.status_code == 403, response.text
    assert transform_specs.get_spec(control_root, {}, "acme", "dummy") is None, "a denied declaration must not reach storage"


def test_an_authz_OUTAGE_fails_closed_rather_than_defaulting_open(client: TestClient, control_root: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A down authz layer must look down. This door writes records naming programs that execute on
    the shared cluster; a permissive fallback here is an arbitrary-code-execution door."""

    async def _outage(*_a: Any, **_k: Any) -> None:
        raise ServiceUnavailableError("openfga is down")

    monkeypatch.setattr(transforms.fga_deps, "require_relation", _outage)

    response = client.post("/v1/project/acme/transform/set", json=VALID)

    assert response.status_code == 503, response.text
    assert transform_specs.get_spec(control_root, {}, "acme", "dummy") is None


def test_the_gate_runs_on_the_PATH_project(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def _record(_client: Any, _settings: Any, _token: Any, *, relation: str, obj: str) -> None:
        seen.append(f"{obj}#{relation}")

    monkeypatch.setattr(transforms.fga_deps, "require_relation", _record)
    client.post("/v1/project/acme/transform/set", json=VALID)

    assert seen == ["project:acme#can_administer"]


# --- delete + list --------------------------------------------------------------------------------


def test_delete_is_idempotent_and_distinguishes_removed_from_absent(client: TestClient) -> None:
    client.post("/v1/project/acme/transform/set", json=VALID)

    first = client.post("/v1/project/acme/transform/delete", json={"name": "dummy"})
    second = client.post("/v1/project/acme/transform/delete", json={"name": "dummy"})

    assert (first.status_code, first.json()["status"]) == (200, "deleted")
    assert (second.status_code, second.json()["status"]) == (200, "absent")


def test_listing_is_scoped_to_the_project(client: TestClient, control_root: str) -> None:
    client.post("/v1/project/acme/transform/set", json=VALID)
    client.post("/v1/project/acme/transform/set", json={**VALID, "name": "second"})
    transform_specs.put_spec(
        control_root,
        {},
        transform_specs.TransformSpec.model_validate({**VALID, "project": "globex", "name": "theirs"}),
    )

    listed = client.get("/v1/projects/acme/transforms").json()

    assert listed["project"] == "acme"
    assert [t["name"] for t in listed["transforms"]] == ["dummy", "second"]


def test_the_OLD_wire_spelling_is_still_accepted(client: TestClient) -> None:
    """This door renamed `lane` to `name` and `entrypoint` to `task`. A caller still sending the old
    spellings must work.

    The frontend ships in the same release and was updated with it, but this door is public and an
    external caller was not. `populate_by_name` plus `AliasChoices` is what makes each
    rename a rename rather than a breaking change — and the models are `extra="forbid"`, so without
    the alias the old body would be REFUSED rather than ignored.

    Asserted through the HTTP door, not on the model, because the model is not what an external
    caller talks to.
    """
    declared = client.post(
        "/v1/project/acme/transform/set",
        json={"lane": "legacy", "from_id": "bronze$events", "to_id": "silver$legacy", "entrypoint": "stage-transform"},
    )
    assert declared.status_code == 200, declared.text
    assert declared.json()["name"] == "legacy", "the old spelling was accepted but answered under a different name"
    assert declared.json()["task"] == "stage-transform", "the old `entrypoint` spelling must land on `task`"


def test_the_REGISTERED_TASKS_are_discoverable_at_the_same_tier(client: TestClient) -> None:
    """A refused field whose vocabulary cannot be read is a trap, not a gate.

    The door refuses an unregistered task correctly; without this listing an operator learns only
    that their guess was wrong. Same `can_administer` tier as declaring — seeing the choices and
    making one are the same act.
    """
    listed = client.get("/v1/projects/acme/tasks")

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["project"] == "acme"
    assert [t["task"] for t in body["tasks"]] == ["dummy-lane", "stage-transform"], "the listing must be ordered and complete"
    dummy = body["tasks"][0]
    assert dummy["engine"] == "ray", "the engine is the executor's, and an operator must see whose task this is"
    assert dummy["command"] == "python /home/ray/jobs/ray_dummy_job.py", "showing the command is not parsing it"
    assert dummy["cardinalities"] == ["1:1"], "the shape a task honours is what makes a refusal predictable"


def test_listing_tasks_is_REFUSED_to_a_non_admin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The vocabulary is not public: a task's `command` names what executes on shared compute."""

    async def _deny(*_a: Any, **_k: Any) -> None:
        raise PermissionDeniedError("nope")

    monkeypatch.setattr(transforms.fga_deps, "require_relation", _deny)

    assert client.get("/v1/projects/acme/tasks").status_code == 403
