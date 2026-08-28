"""DUP-07: the Arrow-IPC stream encoder and its media type have ONE home.

Ten modules hand-rolled the same ``BufferOutputStream`` + ``pa.ipc.new_stream`` + ``write_table``
dance, and seven more hand-typed the ``application/vnd.apache.arrow.stream`` media type. The encoding
is the wire the Lance catalog's write doors parse (a stream, not an IPC file) and the annotation
engine consumes; it belongs to ``service_kit.lancekit.arrow_ipc`` and nowhere else. These gates fail
if any module re-grows a local encoder or re-types the literal.
"""

from __future__ import annotations

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL = _REPO_ROOT / "packages" / "service-kit" / "src" / "service_kit" / "lancekit" / "arrow_ipc.py"
_MEDIA_TYPE = "application/vnd.apache.arrow.stream"


def _source_modules() -> list[Path]:
    roots = [_REPO_ROOT / "services", _REPO_ROOT / "packages"]
    files: list[Path] = []
    for root in roots:
        for p in root.rglob("*.py"):
            parts = set(p.parts)
            if "tests" in parts or p.name.startswith("test_") or p == _CANONICAL:
                continue
            files.append(p)
    return files


def _calls_ipc_stream_writer(source: str) -> bool:
    """True if the module CALLS pa.ipc.new_stream or pa.ipc.RecordBatchStreamWriter (not a comment)."""
    tree = ast.parse(source)
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"new_stream", "RecordBatchStreamWriter"}
        for node in ast.walk(tree)
    )


def test_canonical_encoder_exists() -> None:
    assert _CANONICAL.exists(), "service_kit.lancekit.arrow_ipc must define the one encoder"


def test_no_module_hand_rolls_the_stream_encoder() -> None:
    offenders = [p for p in _source_modules() if _calls_ipc_stream_writer(p.read_text())]
    assert not offenders, f"use service_kit.lancekit.arrow_ipc.encode_arrow_stream instead: {[str(p) for p in offenders]}"


def test_media_type_literal_has_one_definition() -> None:
    offenders = [p for p in _source_modules() if _MEDIA_TYPE in p.read_text()]
    assert not offenders, f"import ARROW_STREAM_MEDIA_TYPE instead of retyping the literal: {[str(p) for p in offenders]}"
