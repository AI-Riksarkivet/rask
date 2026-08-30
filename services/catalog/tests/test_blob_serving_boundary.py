"""The blob-serving seam is its own module, separate from the pylance data plane.

Two different reasons to change live behind one door otherwise. ``dataplane.py`` implements pylance
dataset operations — open the dataset, mutate it, commit, read schema. The blob-serving cluster
implements RFC 9110 HTTP semantics — byte ranges and their clamping, suffix ranges, ``If-Range``
validators, strong ETags, satisfiability, and a bounded streaming window sized for a response body.
Those move when the HTTP spec's read is refined or a client's resume behaviour changes, not when
pylance's dataset API does; ``blob_serving.py`` is where they answer.

The gate is AST over the two source files, so neither a re-export nor an alias can satisfy it: the
names must be DEFINED where they belong. Co-location of ``_BLOB_CHUNK_BYTES`` with ``BlobStream`` is
part of the boundary, not decoration — ``BlobStream.chunks`` reads that constant out of its own
module globals, so a definition parked in the other module is a chunk window that tests can patch
and the stream never reads.
"""

from __future__ import annotations

import ast
import pathlib


_SERVICES = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog" / "services"
_DATAPLANE = _SERVICES / "dataplane.py"
_BLOB_SERVING = _SERVICES / "blob_serving.py"

#: The HTTP-semantics cluster: the served-read value object, its chunk window, and the resolver that
#: turns a Range/If-Range pair into a window over one payload.
_HTTP_SEMANTICS = frozenset({"BlobStream", "_BLOB_CHUNK_BYTES", "read_blob"})


def _defined_names(path: pathlib.Path) -> frozenset[str]:
    """Names the module itself DEFINES at top level — defs, classes and plain assignments."""
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return frozenset(names)


def _bound_names(path: pathlib.Path) -> frozenset[str]:
    """Every name reachable as a module ATTRIBUTE — what it defines plus what it imports.

    Imports count because a re-export is indistinguishable from a definition to
    ``monkeypatch.setattr``: patching a re-exported constant rebinds the alias and leaves the reader
    in the owning module untouched, so the test passes against unpatched behaviour.
    """
    names = set(_defined_names(path))
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return frozenset(names)


def test_the_walk_sees_the_data_plane() -> None:
    assert _DATAPLANE.is_file(), f"{_DATAPLANE} is not there — the gate is measuring nothing"
    assert "create_table" in _defined_names(_DATAPLANE), "the data plane parsed but has no create_table"


def test_the_data_plane_neither_defines_nor_re_exports_the_http_semantics_cluster() -> None:
    leaked = sorted(_HTTP_SEMANTICS & _bound_names(_DATAPLANE))
    assert not leaked, (
        f"HTTP-semantics names reachable on dataplane: {leaked} — they belong to blob_serving.py, and a re-export keeps a stale patch target alive"
    )


def test_the_blob_serving_module_defines_the_http_semantics_cluster() -> None:
    assert _BLOB_SERVING.is_file(), f"{_BLOB_SERVING} does not exist — the blob-serving seam has no module"
    missing = sorted(_HTTP_SEMANTICS - _defined_names(_BLOB_SERVING))
    assert not missing, f"blob_serving.py does not define: {missing}"


def test_the_chunk_window_is_defined_beside_the_stream_that_reads_it() -> None:
    # `BlobStream.chunks` resolves `_BLOB_CHUNK_BYTES` from its own module globals. Split them and a
    # patch of the constant silently stops reaching the loop it is supposed to size.
    assert _BLOB_SERVING.is_file(), f"{_BLOB_SERVING} does not exist — the blob-serving seam has no module"
    names = _defined_names(_BLOB_SERVING)
    assert {"BlobStream", "_BLOB_CHUNK_BYTES"} <= names, "BlobStream and its chunk window are not defined in the same module"
