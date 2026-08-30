"""CAT-CORE-07 — the emitter's optional-metadata keywords are DECLARED once, not copied per class.

``LineageEmitter`` is a four-implementation seam (the Protocol, ``OriginatorBoundEmitter``,
``NoopEmitter``, ``_BaseLineageEmitter``) with two methods each, and every one of the eight spelled out
the same block of optional keywords in full: ``run_id``, ``authorization``, ``source_uri``,
``schema_fields``, ``inputs``, ``extra_run_facets``, ``project``, ``originator``. The block had already
GROWN twice while copied — ``project`` and ``originator`` were added to all eight in one commit — and
the audit's own note records the earlier growth from eleven keywords to twelve.

Copies of a signature fail quietly: an implementation that misses a keyword does not fail to type, it
silently DROPS the field, which for ``project`` means an event nobody watching the tenant is told
about and for ``originator`` means a row in the wrong person's inbox. Both of those were live bugs.

The block is a PEP 692 ``Unpack[TypedDict]`` now, so callers keep the identical keyword call syntax and
every implementation states the bundle by name. Two halves: the count, and the behaviour — a bundle
that typed cleanly but stopped THREADING a field would satisfy the first alone.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from typing import Any, Unpack

from catalog.core.lineage_emit import EmitFields, InputRef, NoopEmitter, OriginatorBoundEmitter, _BaseLineageEmitter


_MODULE = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog" / "core" / "lineage_emit.py"

#: The optional-metadata block that was copied eight times.
_BUNDLE = ("run_id", "authorization", "source_uri", "schema_fields", "inputs", "extra_run_facets", "project", "originator")


#: The eight methods that made up the seam. `build_write_event` (the wire builder) and
#: `emit_write_event` (the endpoint trailer, keyed on `segments`/`delimiter`) are DIFFERENT signatures
#: with their own callers and are deliberately out of scope — the copies were the emitter methods.
_EMIT_METHODS = ("emit_create", "emit_write")


def _parameter_declarations(name: str) -> list[str]:
    tree = ast.parse(_MODULE.read_text())
    return [
        f"{fn.name}:{fn.lineno}"
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and fn.name in _EMIT_METHODS
        for arg in [*fn.args.args, *fn.args.kwonlyargs]
        if arg.arg == name
    ]


def test_the_walk_sees_the_emitter_seam() -> None:
    tree = ast.parse(_MODULE.read_text())
    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert {"LineageEmitter", "OriginatorBoundEmitter", "NoopEmitter", "_BaseLineageEmitter"} <= classes, sorted(classes)


def test_no_emit_method_spells_out_the_optional_bundle() -> None:
    """Eight implementations, one declaration. Each keyword should appear in the shared bundle and in
    no ``emit_create``/``emit_write`` parameter list at all."""
    offences = {name: sites for name in _BUNDLE if (sites := _parameter_declarations(name))}
    assert not offences, (
        "the emitter's optional-metadata keywords are spelled out per implementation — declare the "
        "bundle once and unpack it:\n  " + "\n  ".join(f"{k}: {v}" for k, v in sorted(offences.items()))
    )


class _Recording(_BaseLineageEmitter):
    """A real emitter with a recording transport — the bundle must survive the whole chain."""

    _job_namespace = "test"

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def _send(self, event: dict[str, Any], *, operation: str, table_id: str, authorization: str | None) -> None:
        self.events.append(event)


def _optional() -> EmitFields:
    """Every OPTIONAL field, so a dropped one is visible in the emitted event."""
    return EmitFields(
        run_id="run-1",
        authorization="Bearer x",
        source_uri="s3://bkt/t.lance",
        schema_fields=[],
        inputs=[InputRef("acme", "acme$src", 2)],
        extra_run_facets={"params": {"_producer": "p", "_schemaURL": "s", "lr": "0.1"}},
        project="acme",
        originator="bob",
    )


async def _create(emitter: Any, **optional: Unpack[EmitFields]) -> None:
    await emitter.emit_create(table_id="acme$t", namespace="acme", author="alice", version=4, **optional)


def test_every_field_in_the_bundle_reaches_the_emitted_event() -> None:
    emitter = _Recording()
    asyncio.run(_create(emitter, **_optional()))
    [event] = emitter.events
    facets = event["run"]["facets"]
    assert event["run"]["runId"] == "run-1"
    assert facets["lance"]["project"] == "acme"
    assert facets["lance"]["originator"] == "bob"
    assert facets["params"]["lr"] == "0.1"
    assert event["inputs"][0]["name"] == "acme$src"
    assert event["outputs"][0]["facets"]["dataSource"]["uri"] == "s3://bkt/t.lance"


def test_the_noop_emitter_still_accepts_the_whole_bundle() -> None:
    asyncio.run(_create(NoopEmitter(), **_optional()))
    asyncio.run(NoopEmitter().emit_write(table_id="acme$t", namespace="acme", author="alice", version=4, operation="insert", **_optional()))


def test_the_originator_binding_still_overrides_only_an_absent_claim() -> None:
    inner = _Recording()
    asyncio.run(_create(OriginatorBoundEmitter(inner, "carol"), **_optional()))
    assert inner.events[-1]["run"]["facets"]["lance"]["originator"] == "bob", "an explicit originator must win"
    asyncio.run(_create(OriginatorBoundEmitter(inner, "carol"), **{**_optional(), "originator": None}))
    assert inner.events[-1]["run"]["facets"]["lance"]["originator"] == "carol", "the bound claim must fill an absent one"
