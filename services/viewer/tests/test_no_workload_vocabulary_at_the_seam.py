"""The read plane is a SHARED SEAM, so its vocabulary must survive the question "would this be
right for audio?" — and for images, and for a modality nobody has written yet.

`transcripts.py` failed that test while being, mechanically, entirely agnostic: every field it
touches is resolved from the dataset descriptor (`declared.search.row_table`,
`declared.display.body`, `declared.time.start`, the `alignments` capability), so it already
served any modality that declares them. Only the NAMES were wrong — the module, the router tag,
the route `/api/doc-transcript`, and a `speech_id` path parameter that is nothing more than the
second identity key field.

That is the harder version of the rule to catch, and the reason this gate is a test rather than a
review note. A workload-shaped MECHANISM announces itself: it imports a model, or branches on a
format. A workload-shaped NAME passes every functional test there is, and then teaches the next
reader that this seam is for speech — so the next endpoint gets built for speech too. The
platform's identity erodes one honest-looking name at a time.

The forbidden list is deliberately about SHARED seams only. A sealed `runners/<workload>` may say
whatever it likes; that is what sealing is for.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


#: Vocabulary that names one modality or one workload. Anything here is a defect in a shared seam,
#: even when the code beneath it is modality-agnostic.
FORBIDDEN = (
    "transcript",
    "transcribe",
    "speech",
    "htr",
    "ocr",
    "asr",
    "diarize",
    "voiceprint",
    "handwriting",
)

SEAM = Path(__file__).resolve().parents[1] / "src" / "viewer"


def _public_names(path: Path) -> list[str]:
    """Route paths, function names and the router tag — the surface a reader and a caller SEE."""
    tree = ast.parse(path.read_text())
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.append(node.name)
            found.extend(a.arg for a in node.args.args)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/"):
            found.append(node.value)
        elif isinstance(node, ast.keyword) and node.arg in {"tags", "prefix"}:
            found.extend(el.value for el in ast.walk(node) if isinstance(el, ast.Constant) and isinstance(el.value, str))
    return found


def _seam_files() -> list[Path]:
    return sorted(p for p in SEAM.rglob("*.py") if "__pycache__" not in p.parts)


class TestTheSeamNamesNoWorkload:
    @pytest.mark.parametrize("path", _seam_files(), ids=lambda p: p.name)
    def test_no_module_is_named_after_a_workload(self, path: Path) -> None:
        assert not any(word in path.stem.lower() for word in FORBIDDEN), (
            f"{path.name} names a modality. The read plane serves every modality the descriptor "
            f"can describe; a file named after one teaches the next reader otherwise."
        )

    @pytest.mark.parametrize("path", _seam_files(), ids=lambda p: p.name)
    def test_no_route_path_or_parameter_names_a_workload(self, path: Path) -> None:
        offenders = [n for n in _public_names(path) if any(w in n.lower() for w in FORBIDDEN)]
        assert not offenders, (
            f"{path.name} exposes {offenders} — a route, handler or parameter named after one "
            f"modality. Resolve the concept from the descriptor and name it for what it IS "
            f"(a chunk, a body, an alignment, an identity key), not for the workload that "
            f"happened to produce it first."
        )


class TestTheGateCanActuallyFail:
    """A gate that cannot fail is worse than no gate. Two ways this one could go vacuous: the seam
    path stops resolving, or the AST walk stops finding public names."""

    def test_it_is_reading_a_real_seam(self) -> None:
        assert SEAM.is_dir(), SEAM
        assert len(_seam_files()) > 5

    def test_it_finds_routes_to_check(self) -> None:
        every = [n for p in _seam_files() for n in _public_names(p)]
        assert any(n.startswith("/") for n in every), "the walk found no route paths — it asserts nothing"
