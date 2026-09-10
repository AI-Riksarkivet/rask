"""Every write door that accepts a BRANCH must measure the write on that branch, not on main.

`read_version_and_schema` says what is at stake in its own docstring: "Reading a branch write back off
main pins the WROTE edge to main's version and main's schema — the lineage graph would then record the
evolution as having happened on a version that never carried it." `emit_measured_write` repeats it:
"It must follow the write: a branch has its own version sequence and its own schema."

Three doors in `data.py` accept `branch` and NONE forwarded it to the trailer — the estate's recurring
shape, a convention written down in one module and not held in the one that calls it. Two of them are
worse than versionless: they pass `pin_version=response.version`, a BRANCH version number, into a read
that opens MAIN — so the edge is pinned to whatever main's version of that number happens to be, which
is a different snapshot rather than a missing one.

This asserts the ARGUMENT reaches the trailer rather than driving a real branch, because the defect is
a dropped keyword: a test that only checked the emitted version would pass against a table whose main
and branch sequences happen to agree.
"""

from __future__ import annotations

import ast
from pathlib import Path


_DOORS = {"commit_table_compaction", "insert_into_table", "merge_insert_into_table"}
_SOURCE = Path(__file__).resolve().parents[1] / "src" / "catalog" / "api" / "v1" / "endpoints" / "data.py"


def _handlers() -> dict[str, ast.AsyncFunctionDef]:
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)}


def test_every_door_that_takes_a_branch_forwards_it_to_the_lineage_trailer() -> None:
    handlers = _handlers()
    missing = []
    for name in sorted(_DOORS):
        node = handlers.get(name)
        assert node is not None, f"{name} is gone from data.py — this gate names a door that no longer exists"
        takes_branch = any(arg.arg == "branch" for arg in [*node.args.args, *node.args.kwonlyargs])
        assert takes_branch, f"{name} no longer accepts a branch; drop it from this gate rather than weakening the gate"
        for call in [c for c in ast.walk(node) if isinstance(c, ast.Call)]:
            target = ast.unparse(call.func)
            if target.endswith("emit_measured_write") and not any(kw.arg == "branch" for kw in call.keywords):
                missing.append(name)
    assert not missing, (
        f"these doors measure a BRANCH write against MAIN, so the WROTE edge names a version that never carried the change: {sorted(set(missing))}"
    )


def test_a_pinned_version_never_travels_without_its_branch() -> None:
    """The sharper half. `pin_version` opens the dataset AT that version; a branch's version number
    opened on main is a DIFFERENT snapshot, not an absent one — so a pinned emit that drops the branch
    attaches some other commit's schema to this write."""
    handlers = _handlers()
    offenders = []
    for name, node in handlers.items():
        for call in [c for c in ast.walk(node) if isinstance(c, ast.Call)]:
            if not ast.unparse(call.func).endswith("emit_measured_write"):
                continue
            kwargs = {kw.arg for kw in call.keywords}
            takes_branch = any(arg.arg == "branch" for arg in [*node.args.args, *node.args.kwonlyargs])
            if "pin_version" in kwargs and takes_branch and "branch" not in kwargs:
                offenders.append(name)
    assert not offenders, f"a branch version number is being read back off main in: {sorted(set(offenders))}"
