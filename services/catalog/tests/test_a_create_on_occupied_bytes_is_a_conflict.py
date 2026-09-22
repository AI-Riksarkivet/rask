"""A create landing on an occupied location answers 409 with the location, not 500 with nothing.

[[LH-164]]. Measured on the live catalog 2026-09-21: `POST /v1/table/silver$features/create` answered
**500 `InternalError`, `detail: "Internal Server Error"`**, because `lance.write_dataset` raised
`OSError: Dataset already exists: s3://bind86-wh/medallion/silver` and nothing translated it. The
caller was told nothing at all; the reason existed only in the pod's traceback.

THE SPEC ALREADY SAYS 409. `lance_docs/ns_catalog/spec.yaml:1461` declares `ConflictErrorResponse` on
`CreateTable`, beside 400/401/403/404/503 — so a collision is a modelled outcome of this operation,
not an internal failure. `TableAlreadyExistsError` is the typed error `install_problem_handlers`
translates to it (`fga_deps.py:957`: both AlreadyExists shapes map to 409, spec code 2).

IT IS NOT THE SAME 409 AS A DUPLICATE TABLE, and the detail must not pretend it is. The catalog holds
no record here — the bytes are ungoverned residue at the location the catalog COMPOSES for this id —
so "table already exists" would send the caller looking for a table that does not exist. The location
is the actionable fact, and it is the one thing the 500 withheld.

THE SIBLING TRANSLATION IN THE SAME `except OSError` IS THE MODEL: it matches lance's specific phrase
rather than a broad word, so a genuine infra OSError still surfaces as a 500 rather than being
mislabelled a client error.

NAMING THE LOCATION IS NOT A DISCLOSURE, and it is worth writing down because it looks like one.
`fastapi/exception-handlers.md` says a response body never carries file paths, and the location that
surfaced here was in ANOTHER warehouse's bucket than the caller named. It is still safe: the spec's
own `CreateTableResponse` carries `location` as a plain field, so a SUCCESSFUL create on this exact
call returns the same string. A 409 that withheld it would tell the caller less than the 200 would,
for a request they are already authorized to make. The rule bites on paths the API does not publish —
tracebacks, server filesystem, driver internals — not on a resource location the contract names.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from lance_namespace import TableAlreadyExistsError

from catalog.services import dataplane


def _arrow_table() -> pa.Table:
    return pa.table({"id": pa.array([1, 2, 3], pa.int64())})


def _write_raising(message: str) -> Any:
    def _raise(*args: object, **kwargs: object) -> None:
        raise OSError(message)

    return _raise


def test_a_collision_answers_conflict_and_names_the_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured message, verbatim from the live 500."""
    uri = "s3://bind86-wh/medallion/silver"
    monkeypatch.setattr(dataplane.lance, "write_dataset", _write_raising(f"Dataset already exists: {uri}"))

    with pytest.raises(TableAlreadyExistsError) as caught:
        dataplane._write_blob(_arrow_table(), uri, {}, mode="create", allow_external=False, external_blob_bases=[])

    detail = str(caught.value)
    assert uri in detail, f"the conflict does not name the location it collided on: {detail!r}"


def test_an_unrelated_os_error_is_still_a_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """A translation that swallows every OSError turns real infra failures into client errors — the
    reason the sibling leg in this handler matches a phrase rather than a word."""
    monkeypatch.setattr(dataplane.lance, "write_dataset", _write_raising("Connection reset by peer"))

    with pytest.raises(OSError) as caught:
        dataplane._write_blob(_arrow_table(), "s3://b/t", {}, mode="create", allow_external=False, external_blob_bases=[])

    assert not isinstance(caught.value, TableAlreadyExistsError), "an infra OSError was mislabelled a conflict"


def test_the_error_this_door_raises_is_the_status_the_spec_declares() -> None:
    """THE SECOND HOP, gated on its own. The leg above proves the door raises a typed error; it cannot
    show what the caller receives. A typed error whose code mapped elsewhere would turn a green test
    into a 500 in production — the shape this estate has shipped before."""
    from service_kit.lakehouse.ns_errors import status_for

    # `int(exc.code)` is exactly how the installed handler asks (`ns_errors.py:140`), not a
    # convenient stand-in: a leg that called this differently would prove something production never does.
    assert status_for(int(TableAlreadyExistsError("occupied").code)) == 409, (
        "the conflict this door raises does not reach the caller as the 409 `lance_docs/ns_catalog/spec.yaml:1461` declares on CreateTable"
    )


def test_one_write_door_carries_the_translation_for_all_of_them() -> None:
    """Every governed write in the catalog funnels through `_write_blob`, which is why ONE `except
    OSError` covers them all. A second `write_dataset` elsewhere would answer 500 on the same
    collision while this test stayed green — the half-wiring this estate has shipped before.

    Asserted by ENCLOSING FUNCTION, never by line number: a gate that pins `dataplane.py:290` fails on
    any edit above it, and a gate that cries wolf is one somebody weakens."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "catalog"
    outside: list[str] = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if func.name == "_write_blob":
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "write_dataset":
                    outside.append(f"{path.relative_to(src)}:{node.lineno} in {func.name}()")
    assert not outside, (
        f"the catalog writes datasets outside `_write_blob` at {outside} — those sites do not carry the "
        "conflict translation, so a create colliding there still answers 500 with nothing"
    )
