"""No operation in this service may be one unreadable body (MAINT-09).

`run_sweep` and `compact_one` had grown to 115 and 75 statements at six and five levels of nesting,
each doing in one body what the module's own docstrings describe as a sequence of distinct phases:
discovery, two protective registry reads, a whole-estate pre-pass, trash exclusion, per-dataset policy
resolution, tracing, the blocking Lance/S3 work and the metric aggregation. Every other function in the
package sits at 36 statements or fewer and at most three levels deep, so the two were outliers rather
than the shape of the problem — and they are the two whose failure modes delete data.

Thresholds are set just above the rest of the package: they exist to stop these two from growing back,
not to police code that was never the problem.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from maintenance.core.config import MaintenanceSettings
from maintenance.services import sweep
from maintenance.services.optimize import DatasetResult
from service_kit.lakehouse import maintenance_policies


_SRC = Path(__file__).resolve().parents[1] / "src" / "maintenance" / "services"

_MAX_STATEMENTS = 40
_MAX_NESTING = 4

_NESTS = (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.AsyncFor, ast.AsyncWith)


def _nesting(node: ast.AST, level: int = 0) -> int:
    deepest = level
    for child in ast.iter_child_nodes(node):
        below = level + 1 if isinstance(child, _NESTS) else level
        deepest = max(deepest, _nesting(child, below))
    return deepest


def _functions() -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    return [
        (f"{path.name}:{node.lineno} {node.name}", node)
        for path in sorted(_SRC.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def test_no_function_carries_more_than_forty_statements() -> None:
    offenders = [f"{where} has {n}" for where, node in _functions() if (n := len([s for s in ast.walk(node) if isinstance(s, ast.stmt)])) > _MAX_STATEMENTS]
    assert offenders == [], "god functions:\n  " + "\n  ".join(offenders)


def test_no_function_nests_deeper_than_four_levels() -> None:
    offenders = [f"{where} nests {d} deep" for where, node in _functions() if (d := _nesting(node)) > _MAX_NESTING]
    assert offenders == [], "over-nested functions:\n  " + "\n  ".join(offenders)


# --------------------------------------------------------------------------- #
# The invariants the extraction had to carry OUT of `run_sweep`'s body, pinned
# --------------------------------------------------------------------------- #


def test_a_cadence_skip_does_not_restamp_the_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `policy_interval` skip must NOT record a fresh `last_maintained_at`.

    Inside the old god function this was implicit: the skip `continue`d past the stamp block. Split into
    `_resolve_plan` + `_stamp_cadence` the skip still carries the policy record that produced it, so the
    stamp's own conditions (`policy` present, `compact_interval_hours` set, no error, no refusal) are ALL
    satisfied — and a stamp there pushes the next maintenance out by another full interval every tick,
    freezing the dataset forever behind the mechanism that exists to pace it.
    """
    written: list[str] = []

    def _write(*a: object, **_kw: object) -> None:
        written.append(str(a))

    monkeypatch.setattr(maintenance_policies, "write_state", _write)
    settings = MaintenanceSettings(s3_bucket="b", s3_secret_access_key=SecretStr("unit"))
    policy = {"id": "p1", "compact_interval_hours": 6}
    now = datetime.now(UTC)

    skipped = sweep.DatasetPlan(skipped="policy_interval", policy=policy)
    sweep._stamp_cadence("s3://b/t.lance", skipped, DatasetResult(uri="s3://b/t.lance", skipped="policy_interval"), settings=settings, options={}, now=now)
    assert written == [], "a cadence skip re-stamped the dataset — its next maintenance just moved another interval out"

    maintained = sweep.DatasetPlan(policy=policy)
    sweep._stamp_cadence("s3://b/t.lance", maintained, DatasetResult(uri="s3://b/t.lance", fragments_removed=1), settings=settings, options={}, now=now)
    assert len(written) == 1, "a real maintenance pass must still stamp, or the cadence never advances"
