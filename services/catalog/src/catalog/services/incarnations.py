"""Every project and warehouse record names an incarnation, and every incarnation is born (ADR 0241).

A project's or warehouse's id may be held again by another tenant once it is deleted, so each write of a tuple on or
naming it is fenced by the incarnation its record names and its ``revision`` token in OpenFGA (``fga.open_fence``). A
record without an incarnation, or with one whose birth never landed, can therefore be granted on by no door: a record
stored before incarnations existed (an upgrade starts with every record so), and a record whose create died between
its record and its birth.

This is the invariant's convergence, run by the catalog at boot and on each converge tick (``main``,
``declaration_converge.tick``), so an install or an upgrade from git needs no hand-run step: a record naming no
incarnation is given one by a write conditional on the record as read, which a delete's mark refuses, and a record
whose incarnation is unborn is born (``fga.bear``) with no seed of its own, every tuple already standing on its id
kept. Its doors answer a record not yet converged as one still being established (409 with a ``Retry-After``), never
as absent, and its deletes refuse it the same way, so no delete runs on an id whose incarnation is still being given.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from catalog.services import projects, warehouses
from catalog.services.control_records import INCARNATION, new_incarnation
from lakehouse.governed import fga
from lakehouse.ns_errors import SettlingError
from lakehouse.records import RecordMissingError, mutate_json


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient

    from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: The actor the convergence's writes are recorded as: no person, the catalog converging an invariant of its own.
ACTOR: Final = "system:catalog"
#: Seconds a door asks its caller to wait for a record still being established: the convergence runs at boot and on the
#: next converge tick, and a create still running bears its record within one request.
RETRY_AFTER_SECONDS: Final = 5


class IncarnationsConverged(BaseModel):
    """What one convergence did, each object as ``project:<id>`` / ``warehouse:<id>``."""

    #: Records given an incarnation.
    given: list[str] = Field(default_factory=list)
    #: Incarnations born.
    born: list[str] = Field(default_factory=list)
    #: Objects that could not be converged, with why; the next run tries again.
    failed: list[str] = Field(default_factory=list)


def _with_incarnation(record: dict[str, Any]) -> dict[str, Any]:
    """``record`` naming an incarnation: its own, or a fresh one; a project whose delete has begun is left as it is."""
    if record.get(INCARNATION) or record.get(projects.DELETING_BY):
        return record
    return {**record, INCARNATION: new_incarnation()}


def _give(control_root: str, storage_options: StorageOptions, key: str) -> dict[str, Any] | None:
    """Give the record at ``key`` an incarnation, conditional on the record as read; ``None`` when it is gone."""
    try:
        return mutate_json(control_root, storage_options, key, _with_incarnation)
    except RecordMissingError:
        return None


def _records(control_root: str, storage_options: StorageOptions) -> list[tuple[str, str, dict[str, Any]]]:
    """Each project and warehouse record with its object and its key; a project whose delete has begun is not listed."""
    held: list[tuple[str, str, dict[str, Any]]] = [
        (f"project:{record['id']}", projects.record_key(str(record["id"])), record) for record in projects.list_projects(control_root, storage_options)
    ]
    readable, _unreadable = warehouses.read_warehouses(control_root, storage_options)
    held += [(f"warehouse:{record['id']}", warehouses.record_key(str(record["id"])), record) for record in readable if record.get("id")]
    return held


async def converge(control_root: str, storage_options: StorageOptions, client: OpenFgaClient | None) -> IncarnationsConverged:
    """Give every project and warehouse record an incarnation, and bear every one unborn; what was done.

    Idempotent and safe beside every door: a record's incarnation is written conditionally and never replaced, and a
    birth commits only while its marker is absent, so a create still running and this both bearing one incarnation
    make one birth, and the create then writes its seed under a fence. With authorization off only the records are
    converged.
    """
    done = IncarnationsConverged()
    for obj, key, record in await run_in_threadpool(_records, control_root, storage_options):
        try:
            if not record.get(INCARNATION):
                given = await run_in_threadpool(_give, control_root, storage_options, key)
                if given is None or not given.get(INCARNATION):
                    continue
                record = given
                done.given.append(obj)
            if client is not None and await fga.bear(client, obj, str(record[INCARNATION]), [], actor=ACTOR, origin="registry_rebuild"):  # noqa: TID251
                done.born.append(obj)
        except Exception as exc:  # noqa: BLE001 — one object that cannot be converged must not stop the rest; the next run retries it
            log.warning("incarnation_not_converged", extra={"object": obj, "error": str(exc)[:300]})
            done.failed.append(f"{obj}: {exc}")
    if done.given or done.born or done.failed:
        log.info("incarnations_converged", extra=done.model_dump())
    return done


def require_incarnation(record: dict[str, Any], obj: str) -> str:
    """The incarnation ``record`` names, which every project and warehouse record has once converged.

    Raises:
        SettlingError: The record is not converged yet: the object is still being established.
    """
    if not (incarnation := record.get(INCARNATION)):
        raise establishing(obj)
    return str(incarnation)


def establishing(obj: str) -> SettlingError:
    """The refusal of a door acting on ``obj``, whose record stands and whose incarnation is not yet given or born."""
    return SettlingError(f"{obj} is still being established; ask again shortly", retry_after=RETRY_AFTER_SECONDS)
