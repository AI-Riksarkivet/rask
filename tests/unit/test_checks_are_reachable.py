"""VACUOUS-PASS GATE — a check that passes when its SUBJECT IS ABSENT is not a check.

The estate's 7,405 tests all ask *does this function do what it says*. None of them can ask *is this
check reachable at all*, because a vacuous check is green by construction: it is defeated not by
breaking it but by committing the violation HARDER, until the thing it inspects is no longer there
to inspect.

The measured instance (2026-08-31, `scripts/ray_stage_job.py:571`)::

    parentless = out.count_rows(filter=f"{SOURCE_ROWID_COLUMN} IS NULL") if SOURCE_ROWID_COLUMN in out.schema.names else 0
    _assert_stage_contract(..., parentless=parentless)

`_assert_stage_contract` raises when `parentless` is non-zero. Drop `source_rowid` from the stage
transform entirely and the guard yields 0, the contract passes, and a governed tier accepts rows
with no parent at all — the exact outcome the assertion exists to prevent. Corrupting one row is
caught; deleting the column that proves parentage is not.

WHAT IS FLAGGED, and why the shape is narrow rather than general. Guards that fall back to a benign
value are overwhelmingly legitimate (`raw if ':' in raw else f'user:{raw}'`), so this gate refuses to
reason about "benign fallback" in general and keys on the four properties that together make one a
defeated CHECK:

1. the condition tests membership in a SCHEMA-shaped collection — `.schema.names`, `.column_names`,
   `.columns`, `.fields`, or a bare name saying the same thing. That collection is exactly the thing
   a violator can shrink, which is what makes the guard bypassable rather than merely defensive;
2. the ABSENT branch yields a constant that reads as *nothing to report* — `0`, an empty collection,
   `""`, `None`, `True`;
3. the PRESENT branch MEASURES the very subject the condition asks about — it calls something, and
   the subject appears inside the call. A branch that merely fetches or reformats a value is not a
   measurement and is not flagged;
4. the enclosing function does not ALSO enumerate what is missing from that same collection (a
   `<required> - names` set difference, or `.difference(names)`). A function that already reports
   absence has not been defeated by absence, and its inner guard is a legitimate narrowing.

Four syntactic spellings of the one defect are covered, because writing it across lines must not be
an escape hatch: the ternary (A), an if/else that assigns one name (B), a benign pre-initialisation
followed by a bare guard (C), and a verdict — a raise, or an append to an assertions/violations list
— that only happens INSIDE the presence guard (D). D additionally requires the verdict to be
CONDITIONAL on a measurement, which is what separates "I can only check this when the column is
here" from "the column being here IS the violation" (`if 'tripled' in old.schema.names: raise ...`,
`scripts/ray_lance_job.py:135`, a real check that this gate must not flag).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from itertools import pairwise
from pathlib import Path
from textwrap import dedent
from typing import Final, NamedTuple

import pytest


REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: The planes a governed check can live in. `runners/` is deliberately out: a sealed runner's
#: internals are its own business, and it holds no platform contract for a guard to defeat.
SCANNED_ROOTS: Final = ("services", "packages", "scripts")

#: Attribute names that expose "which columns/fields does this thing actually have". Membership in
#: one of these is the condition a violator defeats by shipping a narrower schema.
_SCHEMA_ATTRS: Final = frozenset({"names", "column_names", "columns", "field_names", "fields", "schema"})

#: Bare locals holding the same collection (`names = set(schema.names)`).
_SCHEMA_NAME_HINTS: Final = ("column", "schema", "field")

#: Collections whose contents ARE the verdict, so an entry not appended is a violation not reported.
_VERDICT_WORDS: Final = ("assertion", "violation", "problem", "finding", "error", "failure", "issue")

_VERDICT_METHODS: Final = frozenset({"append", "add", "extend"})

_EMPTY_FACTORIES: Final = frozenset({"list", "dict", "set", "tuple", "frozenset"})


class Finding(NamedTuple):
    """One vacuous-pass site: where it is, which spelling, and what it yields when the subject is gone."""

    path: str
    line: int
    shape: str
    subject: str
    collection: str
    benign: str
    source: str

    def render(self) -> str:
        return f"{self.path}:{self.line} [{self.shape}] absent {self.subject!r} in {self.collection} yields {self.benign} -> {self.source}"


def _iter_modules() -> Iterator[tuple[str, ast.Module]]:
    for root in SCANNED_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            parts = set(path.parts)
            if ".venv" in parts or "node_modules" in parts or "tests" in parts or path.name.startswith("test_"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # a file this gate cannot read is not a file it may judge
                continue
            yield str(path.relative_to(REPO_ROOT)), tree


def _is_schema_collection(node: ast.expr) -> bool:
    """Does this expression evaluate to "the columns/fields that are actually present"?"""
    inner = node
    while isinstance(inner, ast.Call):
        inner = inner.func
    if isinstance(inner, ast.Attribute):
        return inner.attr in _SCHEMA_ATTRS
    if isinstance(inner, ast.Name):
        lowered = inner.id.lower()
        return lowered == "names" or any(hint in lowered for hint in _SCHEMA_NAME_HINTS)
    return False


class _Membership(NamedTuple):
    subject: str
    collection: str
    body_is_present: bool


def _membership(test: ast.expr) -> _Membership | None:
    """The schema-membership question this condition asks, and which branch runs when the answer is yes.

    A `not` anywhere in the condition is refused rather than reasoned about: mis-reading the polarity
    would invert the verdict, and a missed site is cheaper than a wrong one.
    """
    if any(isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not) for node in ast.walk(test)):
        return None
    for node in ast.walk(test):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], (ast.In, ast.NotIn)) or not _is_schema_collection(node.comparators[0]):
            continue
        return _Membership(ast.unparse(node.left), ast.unparse(node.comparators[0]), isinstance(node.ops[0], ast.In))
    return None


def _benign_label(node: ast.expr) -> str | None:
    """The rendering of a value that reads as "nothing to report", or None if it does not."""
    if isinstance(node, ast.Constant):
        value = node.value
        if value is None or value is True:
            return repr(value)
        if isinstance(value, bool):  # `False` reads as pass or fail depending on the caller — never assume
            return None
        if isinstance(value, (int, float)) and value == 0:
            return repr(value)
        if value == "":
            return "''"
        return None
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and not node.elts:
        return ast.unparse(node) or "set()"
    if isinstance(node, ast.Dict) and not node.keys:
        return "{}"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _EMPTY_FACTORIES and not node.args and not node.keywords:
        return f"{node.func.id}()"
    return None


def _measures(branch: ast.expr, subject: str) -> bool:
    """Does this branch COMPUTE something about the subject, rather than fetch or reformat a value?"""
    return any(isinstance(node, ast.Call) for node in ast.walk(branch)) and subject in ast.unparse(branch)


def _owners(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """Every node mapped to its innermost enclosing function (the module, at top level)."""
    owners: dict[ast.AST, ast.AST] = {}

    def walk(node: ast.AST, owner: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            owners[child] = owner
            walk(child, child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else owner)

    walk(tree, tree)
    return owners


def _enumerates_absence(scope: ast.AST, collection: str) -> bool:
    """Does this function already report what is MISSING from that collection?

    `_TIER_PROVENANCE_COLUMNS - names` names every absent column, so a narrowing guard beneath it
    cannot be defeated by absence — the absence is the finding.
    """
    for node in ast.walk(scope):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub) and ast.unparse(node.right) == collection:
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "difference"
            and any(ast.unparse(a) == collection for a in node.args)
        ):
            return True
    return False


def _assigned_name(stmts: Sequence[ast.stmt]) -> tuple[str, ast.expr] | None:
    """The single `name = value` a branch consists of, if that is all it is."""
    if len(stmts) != 1:
        return None
    stmt = stmts[0]
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0].id, stmt.value
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        return stmt.target.id, stmt.value
    return None


def _verdict_call(call: ast.Call) -> str | None:
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in _VERDICT_METHODS or not isinstance(func.value, ast.Name):
        return None
    lowered = func.value.id.lower()
    return f"{func.value.id}.{func.attr}" if any(word in lowered for word in _VERDICT_WORDS) else None


def _conditional_verdicts(stmts: Sequence[ast.stmt], *, guarded: bool) -> Iterator[str]:
    """Verdicts whose truth depends on a measurement — the ones a missing subject silences.

    An UNCONDITIONAL raise directly under the guard is the opposite shape: there, presence itself is
    the violation, and the check is doing exactly its job.
    """
    for stmt in stmts:
        if isinstance(stmt, ast.If):
            yield from _conditional_verdicts(stmt.body, guarded=True)
            yield from _conditional_verdicts(stmt.orelse, guarded=True)
            continue
        if isinstance(stmt, (ast.For, ast.While, ast.With, ast.Try)):
            yield from _conditional_verdicts(stmt.body, guarded=guarded)
            continue
        if isinstance(stmt, ast.Raise):
            if guarded:
                yield "raise"
            continue
        for call in (node for node in ast.walk(stmt) if isinstance(node, ast.Call)):
            label = _verdict_call(call)
            if label and (guarded or any(isinstance(node, (ast.Compare, ast.BoolOp)) for node in ast.walk(call))):
                yield label


def _statement_blocks(tree: ast.Module) -> Iterator[list[ast.stmt]]:
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield block


def _ternary_findings(path: str, tree: ast.Module, owners: dict[ast.AST, ast.AST]) -> Iterator[Finding]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.IfExp):
            continue
        found = _membership(node.test)
        if found is None:
            continue
        present, absent = (node.body, node.orelse) if found.body_is_present else (node.orelse, node.body)
        benign = _benign_label(absent)
        if benign is None or not _measures(present, found.subject) or _enumerates_absence(owners.get(node, tree), found.collection):
            continue
        yield Finding(path, node.lineno, "A/ternary", found.subject, found.collection, benign, ast.unparse(node))


def _branch_findings(path: str, tree: ast.Module, owners: dict[ast.AST, ast.AST]) -> Iterator[Finding]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not node.orelse:
            continue
        found = _membership(node.test)
        if found is None:
            continue
        present_stmts, absent_stmts = (node.body, node.orelse) if found.body_is_present else (node.orelse, node.body)
        present, absent = _assigned_name(present_stmts), _assigned_name(absent_stmts)
        if present is None or absent is None or present[0] != absent[0]:
            continue
        benign = _benign_label(absent[1])
        if benign is None or not _measures(present[1], found.subject) or _enumerates_absence(owners.get(node, tree), found.collection):
            continue
        yield Finding(path, node.lineno, "B/if-else", found.subject, found.collection, benign, f"{present[0]} = {ast.unparse(present[1])}")


def _preinit_findings(path: str, tree: ast.Module, owners: dict[ast.AST, ast.AST]) -> Iterator[Finding]:
    for block in _statement_blocks(tree):
        for seed, guard in pairwise(block):
            init = _assigned_name([seed])
            if init is None or not isinstance(guard, ast.If) or guard.orelse:
                continue
            benign = _benign_label(init[1])
            found = _membership(guard.test) if benign is not None else None
            if found is None or not found.body_is_present or _enumerates_absence(owners.get(guard, tree), found.collection):
                continue
            rebound = [s for s in guard.body if (assigned := _assigned_name([s])) and assigned[0] == init[0] and _measures(assigned[1], found.subject)]
            if rebound:
                yield Finding(path, guard.lineno, "C/pre-init", found.subject, found.collection, str(benign), ast.unparse(rebound[0]))


def _verdict_findings(path: str, tree: ast.Module, owners: dict[ast.AST, ast.AST]) -> Iterator[Finding]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or node.orelse:
            continue
        found = _membership(node.test)
        if found is None or not found.body_is_present:
            continue
        if not any(found.subject in ast.unparse(stmt) for stmt in node.body):
            continue
        verdicts = sorted(set(_conditional_verdicts(node.body, guarded=False)))
        if not verdicts or _enumerates_absence(owners.get(node, tree), found.collection):
            continue
        yield Finding(path, node.lineno, "D/guarded-verdict", found.subject, found.collection, f"no {'/'.join(verdicts)}", ast.unparse(node.test))


def vacuous_pass_findings() -> list[Finding]:
    """Every site where a check reports "clean" precisely because its subject is gone."""
    findings: list[Finding] = []
    for path, tree in _iter_modules():
        owners = _owners(tree)
        for producer in (_ternary_findings, _branch_findings, _preinit_findings, _verdict_findings):
            findings.extend(producer(path, tree, owners))
    return sorted(findings)


def findings_in_source(source: str, *, path: str = "<probe>") -> list[Finding]:
    """Every detector, over one snippet. The gate's PRECISION is pinned with this rather than argued."""
    tree = ast.parse(dedent(source))
    owners = _owners(tree)
    producers = (_ternary_findings, _branch_findings, _preinit_findings, _verdict_findings)
    return sorted(finding for producer in producers for finding in producer(path, tree, owners))


#: The one defect, written four ways. Each must be caught, or the spelling becomes the escape hatch.
_DEFEATED = {
    "A/ternary": """
        def run(out):
            parentless = out.count_rows(filter=f"{ROWID} IS NULL") if ROWID in out.schema.names else 0
            assert_contract(parentless=parentless)
    """,
    "B/if-else": """
        def run(out):
            if ROWID in out.schema.names:
                parentless = out.count_rows(filter=f"{ROWID} IS NULL")
            else:
                parentless = 0
            assert_contract(parentless=parentless)
    """,
    "C/pre-init": """
        def run(out):
            parentless = 0
            if ROWID in out.schema.names:
                parentless = out.count_rows(filter=f"{ROWID} IS NULL")
            assert_contract(parentless=parentless)
    """,
    "D/guarded-verdict": """
        def run(ds, key_column):
            assertions = []
            if key_column in ds.schema.names:
                assertions.append(Assertion(success=ds.count_rows(f"{key_column} IS NULL") == 0))
            return assertions
    """,
}

#: Shapes that LOOK like the defect and are not. Every one is drawn from a real site in this estate,
#: because a gate is only conservative if it has been shown what it must leave alone.
_LEGITIMATE = {
    "fallback is the subject itself, not a verdict": """
        def strip(table):
            return table.drop_columns([LINEAGE]) if LINEAGE in table.column_names else table
    """,
    "the collection is not a schema": """
        def qualify(raw):
            return raw if ":" in raw else f"user:{raw}"
    """,
    "presence IS the violation, so the raise is unconditional": """
        def check(old):
            if "tripled" in old.schema.names:
                raise SystemExit("old version must still pin the pre-evolution schema")
    """,
    "absence is already enumerated by the same function": """
        def violations(schema):
            names = set(schema.names)
            problems = [f"missing {c!r}" for c in REQUIRED - names]
            if ROWID in names:
                if not pa.types.is_uint64(schema.field(ROWID).type):
                    problems.append("wrong width")
            return problems
    """,
    "the guarded branch fetches a value, it does not measure one": """
        def value(row, table):
            return row[COLUMN] if COLUMN in table.column_names else None
    """,
    "absence returns a fail-SAFE, not a pass": """
        def addable(out):
            if ROWID not in out.column_names:
                return None
            return [name for name in out.column_names]
    """,
}


@pytest.mark.parametrize("shape", sorted(_DEFEATED))
def test_the_gate_catches_every_spelling_of_a_defeated_check(shape: str) -> None:
    findings = findings_in_source(_DEFEATED[shape])
    assert [f.shape for f in findings] == [shape], f"{shape} went undetected: {findings}"


@pytest.mark.parametrize("why", sorted(_LEGITIMATE))
def test_the_gate_leaves_legitimate_guards_alone(why: str) -> None:
    assert findings_in_source(_LEGITIMATE[why]) == [], f"false positive — {why}"


#: Guards whose vacuous branch is COMPENSATED by a caller-facing refusal, keyed `path:line` to the
#: control that compensates. An entry here is a claim that the check is unreachable-but-covered, and the
#: staleness test below refuses one whose compensating control has gone.
#:
#: The distinction this list encodes is the whole point of the gate: skipping a check is fine when
#: SOMEONE ELSE refuses; it is a hole only when nobody does.
COMPENSATED: dict[str, str] = {
    "packages/service-kit/src/service_kit/lakehouse/quality.py:135": "refuse_a_gate_that_cannot_run",
    "packages/service-kit/src/service_kit/lakehouse/quality.py:189": "refuse_a_gate_that_cannot_run",
}


def test_a_compensating_control_still_exists() -> None:
    """An exemption that outlives its compensating control is worse than no exemption.

    It reads as "someone thought about this" while the reason has silently gone — which is exactly how
    the estate's stale prose keeps producing false confidence.
    """
    source = (REPO_ROOT / "services/catalog/src/catalog/services/publication.py").read_text()
    for site, control in sorted(COMPENSATED.items()):
        assert f"def {control}(" in source, f"{site} is exempt because {control!r} refuses, and that function no longer exists"
        assert f"{control}(" in source.split(f"def {control}(", 1)[1], f"{control} is defined but never called — {site}'s exemption is hollow"


def test_no_check_passes_because_its_subject_is_absent() -> None:
    findings = [f for f in vacuous_pass_findings() if f"{f.path}:{f.line}" not in COMPENSATED]
    assert not findings, "checks defeated by dropping the very column they inspect:\n" + "\n".join(f.render() for f in findings)
