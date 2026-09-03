"""The generated OpenAPI must be stable and its operationIds unique.

`make openapi-check` regenerates the spec and diffs it against the committed one, so a generator that
is not deterministic makes the gate flip-flop — green on one run, red on the next, for a tree nobody
touched. That happened on 2026-09-02: dual-mounting `count_rows` and `tags/list` with a single
`api_route(methods=["GET", "POST"])` emits ONE operationId for BOTH methods, and FastAPI derives its
suffix from whichever method it registered last, so the committed spec disagreed with the regenerated
one on nothing but `_get` versus `_post`.

A shared operationId is also invalid OpenAPI in its own right — the field is required to be unique
across the document, and every generator keys its client method names on it, so two operations sharing
one id silently collide in whatever the generator produces.

Both properties are asserted here rather than left to the diff gate: the gate can only say the spec
moved, not which of these went wrong.
"""

from __future__ import annotations

import collections
import json
import pathlib


_SPECS = (
    pathlib.Path(__file__).resolve().parents[2] / "docs" / "catalog-openapi.json",
    pathlib.Path(__file__).resolve().parents[2] / "docs" / "lineage-openapi.json",
)
_METHODS = frozenset({"get", "put", "post", "delete", "patch", "head", "options"})


def _operations(spec: dict[str, object]) -> list[tuple[str, str, dict[str, object]]]:
    out = []
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return out
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() in _METHODS and isinstance(op, dict):
                out.append((method, str(path), op))
    return out


def test_every_operation_id_is_unique() -> None:
    for spec_path in _SPECS:
        spec = json.loads(spec_path.read_text())
        ops = _operations(spec)
        assert ops, f"{spec_path.name} holds no operations — the walk is wrong, not the spec"
        ids = [str(op.get("operationId")) for _m, _p, op in ops if op.get("operationId")]
        duplicates = {i: n for i, n in collections.Counter(ids).items() if n > 1}
        assert not duplicates, (
            f"{spec_path.name} reuses operationIds {sorted(duplicates)} — invalid OpenAPI, and every "
            "generator keys its client method names on this field, so the operations collide silently. "
            "A route dual-mounted with `api_route(methods=[...])` is the way this happens; use one "
            "decorator per method with an explicit `operation_id`."
        )


def test_every_operation_declares_an_id() -> None:
    """An absent id makes the generator invent one from the path, which then moves whenever the path
    or the function name does — the same instability by another route."""
    for spec_path in _SPECS:
        spec = json.loads(spec_path.read_text())
        missing = [f"{m.upper()} {p}" for m, p, op in _operations(spec) if not op.get("operationId")]
        assert not missing, f"{spec_path.name}: operations with no operationId: {missing}"
