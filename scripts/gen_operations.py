#!/usr/bin/env python
"""Generate ``docs/operations.json``: every operation the catalog and lineage serve, with what decides who may call it.

One row per operation of each service's OpenAPI document. A catalog row's gate is read from the tables and functions
``catalog.api.fga_deps`` decides a request with, so the file moves when a rung map does. ``make openapi`` writes it
and ``make openapi-check`` fails on drift. Run: ``uv run scripts/gen_operations.py``.

A gate is one of:

- ``open``: the operation asks for no credential (the probes);
- ``relation``: the router asks ``relation`` on the object the path's id names;
- ``create_on_parent``: the router asks ``relation`` on the parent of the object the door makes;
- ``transaction``, ``classification``: the router decides in a branch of its own, on the object the id names;
- ``grant``, ``batch``: the router authorizes from the request body;
- ``handler``: the router asks for authentication only, and the handler decides;
- ``undeclared``: no rung map declares the door, which the router refuses for every caller.

``also_asks`` lists the relations a request's own content can move the gate to (a rename into another namespace, a
credential vended at a writing tier). ``write_judge`` is true where the write judge decides the door. Every lineage
operation that asks for a credential is ``handler``: lineage gates each door in its own dependency.

The branch order in :func:`_catalog_gate` restates ``fga_deps._decide`` and ``fga_deps._write_door``; the relations,
suffix sets and path parsing are theirs.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel


_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "services"))  # `uv run` doesn't apply the pytest pythonpath
os.environ.setdefault("LANCE_S3_ACCESS_KEY_ID", "spec")
os.environ.setdefault("LANCE_S3_SECRET_ACCESS_KEY", "spec")

_OUT = _ROOT / "docs" / "operations.json"
_METHODS = ("get", "post", "put", "delete", "patch", "head")

#: A name every guarded resource accepts: a warehouse, a namespace in it and an object in that.
_SAMPLE = ["warehouse", "namespace", "object"]
#: The same object's name in another namespace, which is what makes a rename a move.
_MOVED = ["warehouse", "elsewhere", "object"]

Gate = Literal["open", "relation", "create_on_parent", "transaction", "classification", "grant", "batch", "handler", "undeclared"]


class Decision(BaseModel):
    """What stands in front of an operation."""

    gate: Gate
    relation: str | None = None
    asked_on: str | None = None
    also_asks: list[str] = []
    write_judge: bool = False


class Operation(Decision):
    """One served operation and the gate in front of it."""

    method: str
    path: str
    operation_id: str
    group: str
    #: The Lance Namespace spec defines the operation; false for one only this estate serves.
    spec: bool


def _catalog_gate(method: str, path: str) -> Decision:
    """The gate the catalog's router puts in front of ``method path``, a route's path format."""
    from lance_namespace.errors import InternalError

    from catalog.api import fga_deps
    from catalog.core.identifiers import CATALOG_DELIMITER

    if "{id}" not in path:
        return Decision(gate="batch" if path.rstrip("/") in fga_deps._BATCH_PATHS else "handler")
    object_id = CATALOG_DELIMITER.join(_SAMPLE)
    concrete = path.replace("{id}", object_id)
    resource = fga_deps._resource_for(concrete)
    if resource is None:
        return Decision(gate="handler")
    suffix = fga_deps._suffix(concrete, resource, object_id)
    judged = resource in fga_deps._JUDGED_RESOURCES
    if suffix in fga_deps._CREATE_ON_PARENT_SUFFIXES.get(resource, frozenset()):
        rung = fga_deps._create_parent_rung(resource, _SAMPLE)
        return Decision(gate="create_on_parent", relation=rung.relation if rung is not None else None, asked_on="parent", write_judge=judged)
    if resource == "transaction":
        return Decision(gate="transaction" if suffix in fga_deps._TRANSACTION_SUFFIXES else "undeclared", asked_on="table")
    if suffix in fga_deps._GRANT_SUFFIXES:
        return Decision(gate="grant")
    fga_type = fga_deps._FGA_TYPE[resource]
    if fga_type == "classification":
        return Decision(gate="classification", asked_on=fga_type)
    try:
        relation = fga_deps._action_relation(fga_type, suffix, method=method)
    except InternalError:
        return Decision(gate="undeclared", asked_on=fga_type)
    also_asks: set[str] = set()
    if judged and suffix == "credentials":
        also_asks.update(fga_deps.VEND_TIER_RUNGS.values())
    if judged and suffix == "rename":
        also_asks.add(fga_deps._rename_relation(_SAMPLE, _MOVED))
    return Decision(
        gate="relation",
        relation=relation,
        asked_on=fga_type,
        also_asks=sorted(also_asks - {relation}),
        write_judge=judged and relation not in fga_deps._READ_RELATIONS,
    )


def _operations(module_path: str, *, catalog: bool) -> list[Operation]:
    """Every operation of the app at ``module_path``, ordered by path and method."""
    from lakehouse.spec_routes import is_spec_route

    spec = importlib.import_module(module_path).app.openapi()
    rows: list[Operation] = []
    for path, item in spec["paths"].items():
        for method in _METHODS:
            if (operation := item.get(method)) is None:
                continue
            if not operation.get("security"):
                decision = Decision(gate="open")
            elif catalog:
                decision = _catalog_gate(method.upper(), path)
            else:
                decision = Decision(gate="handler")
            rows.append(
                Operation.model_validate(
                    decision.model_dump()
                    | {
                        "method": method.upper(),
                        "path": path,
                        "operation_id": operation["operationId"],
                        "group": operation["tags"][0],
                        "spec": catalog and is_spec_route(method, path),
                    }
                )
            )
    return sorted(rows, key=lambda row: (row.path, row.method))


def main() -> None:
    services = {
        "catalog": _operations("catalog.main", catalog=True),
        "lineage": _operations("lineage.main", catalog=False),
    }
    document = {name: [row.model_dump() for row in rows] for name, rows in services.items()}
    _OUT.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    for name, rows in services.items():
        print(f"wrote {len(rows)} {name} operations to docs/{_OUT.name}")


if __name__ == "__main__":
    main()
