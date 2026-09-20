"""No module or identifier under `services/medallion` is named for a workload ([[LH-010]]).

rask is an agnostic multimodal lakehouse, and the cascade is the seam where that is easiest to lose:
a transform has to know SOMETHING about the rows it moves, and the shortest path to making it work is
to name the thing it was first made to work for. The estate has already paid for that once —
`medallion/schemas/htr.py` carried a per-workload gold contract (nine of eleven columns describing
transcribed page images) and was deleted 2026-08-17 for "making the cascade a transcription pipeline
wearing a lakehouse's name". Nothing prevented it, and nothing prevents the next one.

DERIVED FROM `runners/`, never a hardcoded list, which is the whole point: a tenth workload is a new
sibling directory and inherits this gate without an edit here. That is the same shape
`test_the_maintenance_doors_refuse_a_branch_they_cannot_honour` uses, and for the same reason — a list
someone must remember to extend is a list that goes stale on exactly the workload nobody expected.

NAMES ONLY, NEVER PROSE, and the distinction is deliberate rather than a weakening. A comment reading
"an audio deriver slots into `_DERIVERS` later" (`derivers.py:10`) is the agnostic argument being MADE
— it says the dispatch table takes another modality — and a gate that failed on it would train people
to delete the sentences that keep the design honest. What must not exist is a file or an identifier
the platform carries that only one workload could ever mean.

THE SIBLING GATES COVER THE CHART, NOT THIS. `test_invariants.py` already refuses a workload name in
`OTEL_SERVICE_NAME`, in `serveConfigV2` and in the Ray dashboard's panel queries — measured, all three
read deployment YAML. The medallion's own source tree was covered by none of them.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest


_MEDALLION = pathlib.Path(__file__).resolve().parents[2] / "services" / "medallion" / "src"
_RUNNERS = pathlib.Path(__file__).resolve().parents[2] / "runners"


def _workloads() -> frozenset[str]:
    """Every sealed runner's directory name — the vocabulary the platform may not adopt."""
    return frozenset(p.name.lower() for p in _RUNNERS.iterdir() if p.is_dir() and not p.name.startswith("."))


def _segments(name: str) -> set[str]:
    """`name` split into the words it is built from, so matching is on WHOLE segments.

    Substring matching would be unusable here: `kg` sits inside `background`, `asr` inside `parser`.
    Splitting on `_`/`-`/`.` and on camelCase boundaries means `KgExtractor` and `kg_rows` are caught
    while `background_task` is not.
    """
    return {s.lower() for s in re.split(r"[_\-.]", name) if s} | {w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)}


def _named_nodes(tree: ast.AST) -> list[tuple[int, str]]:
    """Declared names: classes, functions, and module/class-level assignment targets."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.append((node.lineno, node.name))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            found.append((node.lineno, node.id))
    return found


def test_there_are_workloads_to_check_against() -> None:
    """Without this the whole suite passes by intersecting with an empty set."""
    assert len(_workloads()) >= 5, f"only found {sorted(_workloads())} under runners/ — this gate would check nothing"


def test_the_segmenter_actually_matches_a_workload_name() -> None:
    """THE CONTROL, and it is load-bearing because the tree is clean.

    A gate that finds nothing looks identical whether the estate is agnostic or the matcher is broken.
    These are the shapes the deleted `schemas/htr.py` and its plausible successors would take.
    """
    workloads = _workloads()

    assert _segments("htr") & workloads
    assert _segments("htr_rows") & workloads
    assert _segments("AsrTranscript") & workloads
    assert _segments("build_voiceprint_index") & workloads
    # ...and the false positives it must NOT produce, or the gate is one people disable.
    assert not _segments("background_task") & workloads, "`kg` matched inside `background`"
    assert not _segments("parser") & workloads, "`asr` matched inside `parser`"


@pytest.mark.parametrize("path", sorted(_MEDALLION.rglob("*.py")), ids=lambda p: p.name if isinstance(p, pathlib.Path) else "")
def test_no_medallion_MODULE_is_named_for_a_workload(path: pathlib.Path) -> None:
    """`schemas/htr.py` is the file this exists to prevent coming back."""
    offending = _segments(path.stem) & _workloads()

    assert not offending, f"{path.name} is named for the {sorted(offending)} workload — a workload's shape belongs in its sealed runner"


@pytest.mark.parametrize("path", sorted(_MEDALLION.rglob("*.py")), ids=lambda p: p.name if isinstance(p, pathlib.Path) else "")
def test_no_medallion_IDENTIFIER_is_named_for_a_workload(path: pathlib.Path) -> None:
    """A workload-shaped class or constant is the same defect one level down from the filename.

    `HtrGoldRow` in `tier.py` would pass the module check and be exactly what the deleted file was.
    """
    workloads = _workloads()
    offending = [f"{path.name}:{line} {name}" for line, name in _named_nodes(ast.parse(path.read_text())) if _segments(name) & workloads]

    assert offending == [], f"these name a workload inside the platform: {offending}"
