"""The task registry — what may be run, written by the plane that can run it and merely CONSULTED here.

docs/adr/0052-the-compute-plane-is-decoupled-a-port-two-adapters-and-no.md "The compute plane is decoupled", step 1 of the owner-ordered §7.4.

A transform declares a TASK: a record under `<control_root>/_tasks/<task hash>/<engine hash>.json` naming an
engine, a command that engine understands, and the shapes it supports. Each engine that can run a task
registers its own record, and the declaration chooses between them. The platform never parses the command
and never learns the engine's vocabulary — which is what lets a second engine be declared without a
catalog change, and keeps the engine noun out of the OpenAPI every API client reads.

The registration carries two checks a path-shaped allowlist cannot make: **registered for an engine
this estate runs**, and **supports the declared cardinality**. Both are refused at the declaration
door, so an undeclarable transform can never be submitted at all.

WHY AN OBJECT-STORE RECORD rather than an endpoint on the submit plane: serving the registry from
`compute` would give the catalog an outbound dependency on the submit plane and a fail-closed policy
question on every declaration. The object form has neither, and matches three existing precedents in
this directory (`transform_specs`, `gate_specs`, `maintenance_policies`) — same one-writer/one-reader
shape, same `fs_and_base` seam.

**`service-kit` must not gain a `ray` dependency.** This module adds none: the engine is a STRING that
some other plane writes, which is the entire point.

All IO is blocking; callers threadpool it.
"""

from __future__ import annotations

import hashlib
import json
import logging

import pyarrow.fs as pafs
from pydantic import BaseModel, ConfigDict, Field

from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)

#: Where registrations live under the control root. A sibling of `_specs/`, `_gates/`, `_policies/`.
TASKS_PREFIX = "_tasks"


class TaskRegistration(BaseModel):
    """One runnable task, described by the plane that can actually run it.

    `extra="forbid"` like every sibling record: a misspelled field must not be silently dropped into a
    registration that then means something other than what its author wrote.
    """

    model_config = ConfigDict(extra="forbid")

    #: The key a `TransformSpec` names. Opaque to the platform and to the catalog.
    task: str = Field(min_length=1)
    #: "ray" | "inprocess" | "spark" | … — a string this package never interprets.
    engine: str = Field(min_length=1)
    #: The ENGINE's business: a command line, an image, a class path. The platform does not parse it. Empty for an
    #: out-of-process engine, which resolves the task to its own command and tells the lakehouse none.
    command: str = ""
    #: The build this registration describes, so a stale record is detectable.
    code_version: str = ""
    #: Which `stage_stamp.CARDINALITIES` this task can honour. EMPTY MEANS ALL — a task that declares
    #: nothing constrains nothing, or every existing registration would have to enumerate the
    #: vocabulary just to keep working.
    cardinalities: list[str] = Field(default_factory=list)
    #: Which of O1..O12 (§2.5) this task CLAIMS to satisfy. A claim, never a proof: the platform
    #: re-derives the obligations from the written dataset, which is the difference between a contract
    #: and a convention.
    obligations: list[str] = Field(default_factory=list)

    def honours(self, cardinality: str) -> bool:
        """May this task be declared for that cardinality?"""
        return not self.cardinalities or cardinality in self.cardinalities


def _task_dir(task: str) -> str:
    """The directory every engine's registration of one task lives in. The task id is author-supplied, so
    hash rather than build a path out of it — the same rule `transform_specs._key` states."""
    return f"{TASKS_PREFIX}/{hashlib.sha256(task.encode()).hexdigest()[:24]}"


def _key(task: str, engine: str) -> str:
    """One record per (task, engine): two engines registering the same task each keep their own record, and
    a declaration chooses between them. Keyed on the task alone, the second registrar overwrote the first."""
    return f"{_task_dir(task)}/{hashlib.sha256(engine.encode()).hexdigest()[:24]}.json"


def put_task(control_root: str, storage_options: StorageOptions, registration: TaskRegistration) -> None:
    """Register one task for its engine (overwrite — registering is idempotent)."""
    fs, base = fs_and_base(control_root, storage_options)
    key = _key(registration.task, registration.engine)
    fs.create_dir(f"{base}/{key}".rsplit("/", 1)[0], recursive=True)
    with fs.open_output_stream(f"{base}/{key}") as stream:
        stream.write(registration.model_dump_json().encode("utf-8"))


def get_task(control_root: str, storage_options: StorageOptions, task: str, engine: str) -> TaskRegistration | None:
    """The task's registration for that engine, or ``None`` when that engine never registered it.

    ``None`` rather than a default, the contract `get_spec` already states: a caller must be able to
    tell "nobody registered this" from "this is configured", so an unknown task is refused at the door
    instead of submitting something for a name that was probably a typo.
    """
    fs, base = fs_and_base(control_root, storage_options)
    try:
        stream = fs.open_input_stream(f"{base}/{_key(task, engine)}")
    except FileNotFoundError:
        return None
    with stream:
        raw = stream.readall().decode("utf-8")
    return _parse(raw, path=_key(task, engine))


def registrations_of(control_root: str, storage_options: StorageOptions, task: str) -> list[TaskRegistration]:
    """Every engine's registration of one task, sorted by engine; empty when no engine registered it."""
    fs, base = fs_and_base(control_root, storage_options)
    found = _read_all(fs, pafs.FileSelector(f"{base}/{_task_dir(task)}", allow_not_found=True, recursive=False))
    return sorted((r for r in found if r.task == task), key=lambda r: r.engine)


def delete_task(control_root: str, storage_options: StorageOptions, task: str, engine: str) -> bool:
    """Unregister one engine's task; ``False`` when there was none (delete is idempotent)."""
    fs, base = fs_and_base(control_root, storage_options)
    try:
        fs.delete_file(f"{base}/{_key(task, engine)}")
    except FileNotFoundError:
        return False
    return True


def list_tasks(control_root: str, storage_options: StorageOptions) -> list[TaskRegistration]:
    """Every readable registration (unordered), one per (task, engine).

    ESTATE-WIDE, not per-tenant: a task names what some plane can run, and the same Ray cluster runs
    it for every project. Scoping the records per project would make the same capability need one
    registration per tenant, and a tenant added later would silently be unable to declare anything.

    One corrupt record is SKIPPED with a warning rather than voiding the rest — the rule `list_specs`
    states: a listing that silently emptied would read as "this estate can run nothing" while the
    tasks keep running. Only records at `_tasks/<task>/<engine>.json` are read, so a file directly under
    `_tasks/` is not a registration.
    """
    fs, base = fs_and_base(control_root, storage_options)
    root = f"{base}/{TASKS_PREFIX}"
    selector = pafs.FileSelector(root, allow_not_found=True, recursive=True)
    return _read_all(fs, selector, depth=2, root=root)


def _read_all(fs: pafs.FileSystem, selector: pafs.FileSelector, *, depth: int = 1, root: str = "") -> list[TaskRegistration]:
    out: list[TaskRegistration] = []
    for info in fs.get_file_info(selector):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        if root and info.path.removeprefix(root).strip("/").count("/") != depth - 1:
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                raw = stream.readall().decode("utf-8")
        except Exception as exc:  # noqa: BLE001 — one unreadable record must not void the listing
            log.warning("task_registration_unreadable", extra={"path": info.path, "error": str(exc)})
            continue
        registration = _parse(raw, path=info.path)
        if registration is not None:
            out.append(registration)
    return out


def _parse(raw: str, *, path: str) -> TaskRegistration | None:
    """An unreadable registration is ``None`` and a WARNING, never an exception.

    One corrupt record must not fail a declaration door for every other task, and it must not read as
    "unregistered" silently either — the log line is what distinguishes the two.
    """
    try:
        return TaskRegistration.model_validate(json.loads(raw))
    except Exception as exc:  # noqa: BLE001 — a corrupt record must not end the caller's request
        log.warning("task_registration_unreadable", extra={"path": path, "error": str(exc)})
        return None
