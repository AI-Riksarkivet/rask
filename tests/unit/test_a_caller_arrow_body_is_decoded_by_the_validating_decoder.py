"""XC-097: Arrow IPC is decoded in ONE place in the fleet, and that place validates what it decodes in full.

IPC framing that parses says nothing about the buffers it frames. Decoded without
`Table.validate(full=True)`, an offset past its values buffer reads process memory (which an
annotation import then stores and returns) and an offset that decreases aborts the process
(measured on pyarrow 25.0.0). So every Arrow IPC read under `services/*/src` and `packages/*/src`
goes through `service_kit.lancekit.arrow_ipc`, with no exception, and these gates refuse:

- a reference to an IPC reader anywhere else — called, passed as a value, or imported by name, so a
  module that re-exports a reader is refused where it imports it;
- a module that bears readers (`pyarrow`, `pyarrow.ipc`, `pyarrow.feather`, …) used as a VALUE —
  returned, passed, stored on an object, put in a tuple or a default — because the scan cannot follow
  a module once it escapes; dereferencing it (`pa.Table`) or aliasing it to a plain name
  (`ipc = pa.ipc`, which the scan follows) is fine;
- a literal `importlib.import_module`, `__import__` or `sys.modules[...]` naming such a module;
- a reader reference in the canonical module whose table does not pass through `_validated`;
- a `_validated` that does not call `validate(full=True)` — plain `validate()` accepts decreasing
  offsets and string values that are not UTF-8.

Readers are pyarrow's public and private IPC entry points (Feather v2 is the IPC file format, and
`pyarrow.dataset` over an IPC format reads it), `pyarrow.flight`, and the wrappers that decode IPC
bytes in libraries the fleet resolves: `pandas.read_feather`, lancedb's namespace helper, and polars'
IPC readers. Bindings are resolved across fleet modules, so `from <fleet module> import pa` is
followed to what that module bound. Outside the scan: bytes handed to a native reader (the catalog's
`native.call`), a module name computed at runtime, `eval`/`exec`, and duckdb, which reads IPC only
through an extension the fleet does not install.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL = Path("packages/service-kit/src/service_kit/lancekit/arrow_ipc.py")

#: The libraries whose bindings the scan follows.
_TRACKED_ROOTS = frozenset({"pyarrow", "pandas", "lancedb", "polars"})

#: Every entry point that reads Arrow IPC bytes, by qualified name. A reference to one is a read
#: wherever it sits: `MessageReader.open_stream(b)` references `MessageReader`.
_READERS = frozenset(
    {
        *(
            f"pyarrow.ipc.{name}"
            for name in (
                "open_stream",
                "open_file",
                "RecordBatchStreamReader",
                "RecordBatchFileReader",
                "MessageReader",
                "read_message",
                "read_record_batch",
                "read_schema",
                "read_tensor",
                "deserialize_pandas",
            )
        ),
        *(f"pyarrow.{name}" for name in ("RecordBatchStreamReader", "RecordBatchFileReader", "MessageReader", "deserialize_pandas")),
        *(f"pyarrow.lib.{name}" for name in ("MessageReader", "read_message", "read_record_batch", "read_schema", "read_tensor")),
        *(f"pyarrow.feather.{name}" for name in ("read_table", "read_feather", "FeatherDataset")),
        *(f"pyarrow.dataset.{name}" for name in ("IpcFileFormat", "FeatherFileFormat")),
        "pandas.read_feather",
        "lancedb.namespace._arrow_ipc_to_record_batch_reader",
        *(f"polars.{name}" for name in ("read_ipc", "read_ipc_stream", "scan_ipc", "read_ipc_schema")),
    }
)

#: Modules that bear readers; one of these used as a value takes its readers where the scan cannot follow.
_READER_MODULES = frozenset({path.rsplit(".", 1)[0] for path in _READERS} | {"pyarrow", "pyarrow.flight"})

#: `pyarrow.dataset.dataset` reads IPC unless its format is absent (Parquet) or names another format.
_DATASET = "pyarrow.dataset.dataset"
_IPC_FORMATS = frozenset({"arrow", "ipc", "feather"})
_OTHER_FORMAT_CLASSES = frozenset(f"pyarrow.dataset.{name}" for name in ("ParquetFileFormat", "CsvFileFormat", "JsonFileFormat", "OrcFileFormat"))

_DYNAMIC_IMPORTS = frozenset({"importlib.import_module", "__import__"})


def _is_tracked(module: str) -> bool:
    return module.split(".", 1)[0] in _TRACKED_ROOTS


def _is_reader(path: str | None) -> bool:
    """A named reader, anything in `pyarrow.flight`, or any private pyarrow name (`pyarrow._feather`, `pyarrow.lib._Rec…`)."""
    if path is None:
        return False
    if path in _READERS or path == "pyarrow.flight" or path.startswith("pyarrow.flight."):
        return True
    return path.startswith("pyarrow.") and any(part.startswith("_") for part in path.split(".")[1:])


def _is_reader_module(path: str | None) -> bool:
    return path is not None and (path in _READER_MODULES or (path.startswith("pyarrow.") and _is_reader(path)))


#: A fleet module, as the binding a name in another module resolves to: `@service_kit.lancekit.reader`.
_FLEET = "@"


def _module_name(relative: Path) -> str:
    """`packages/<pkg>/src/a/b.py` -> `a.b`; an `__init__.py` names its package."""
    parts = relative.with_suffix("").parts[3:]
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _absolute(module: str | None, level: int, importer: str, is_package: bool) -> str | None:
    if level == 0:
        return module
    base = importer.split(".") if is_package else importer.split(".")[:-1]
    base = base[: len(base) - (level - 1)] if level > 1 else base
    return ".".join([*base, module] if module else base)


class _Fleet:
    """Every fleet module's top-level bindings, resolved across `from <fleet module> import x` to a fixpoint."""

    def __init__(self, sources: dict[Path, str]) -> None:
        self.trees = {path: ast.parse(text) for path, text in sources.items()}
        self.names = {path: _module_name(path) for path in sources}
        self.packages = {path: path.name == "__init__.py" for path in sources}
        self.modules = set(self.names.values())
        self.exports: dict[str, dict[str, str]] = {name: {} for name in self.modules}
        changed = True
        while changed:
            changed = False
            for path, tree in self.trees.items():
                bound = _Bindings(self, self.names[path], self.packages[path], tree.body).bound
                exports = self.exports[self.names[path]]
                for name, target in bound.items():
                    if exports.get(name) != target:
                        exports[name] = target
                        changed = True

    def attribute(self, module: str, attr: str) -> str | None:
        if attr in self.exports.get(module, {}):
            return self.exports[module][attr]
        return f"{_FLEET}{module}.{attr}" if f"{module}.{attr}" in self.modules else None


class _Bindings:
    """What each name in `statements` is bound to: a tracked path (`pyarrow.ipc`) or a fleet module (`@a.b`)."""

    def __init__(self, fleet: _Fleet | None, importer: str, is_package: bool, statements: list[ast.stmt]) -> None:
        self.fleet, self.importer, self.is_package = fleet, importer, is_package
        self.bound: dict[str, str] = {}
        for node in statements:
            for inner in ast.walk(node):
                self._bind_import(inner)
        self._bind_aliases(statements)

    def _bind_import(self, node: ast.AST) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_tracked(alias.name):
                    # `import pyarrow.ipc` binds `pyarrow`; `import pyarrow.ipc as x` binds `x` to the submodule.
                    self.bound[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
                elif self.fleet is not None and alias.name in self.fleet.modules:
                    self.bound[alias.asname or alias.name.split(".")[0]] = f"{_FLEET}{alias.name if alias.asname else alias.name.split('.')[0]}"
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node.module, node.level, self.importer, self.is_package)
            if module is None:
                return
            for alias in node.names:
                if alias.name == "*":
                    continue
                if _is_tracked(module):
                    self.bound[alias.asname or alias.name] = f"{module}.{alias.name}"
                elif self.fleet is not None and module in self.fleet.modules:
                    target = self.fleet.attribute(module, alias.name)
                    if target is not None:
                        self.bound[alias.asname or alias.name] = target

    def _bind_aliases(self, statements: list[ast.stmt]) -> None:
        """`ipc = pa.ipc`, to a fixpoint: a plain-name alias of a module is followed, not refused."""
        assignments = [
            (targets, value)
            for node in statements
            for inner in ast.walk(node)
            for targets, value in (
                [(inner.targets, inner.value)]
                if isinstance(inner, ast.Assign)
                else [([inner.target], inner.value)]
                if isinstance(inner, ast.AnnAssign) and inner.value is not None
                else []
            )
        ]
        changed = True
        while changed:
            changed = False
            for targets, value in assignments:
                path = self.qualified(value)
                if path is None or _is_reader(path):
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and self.bound.get(target.id) != path:
                        self.bound[target.id] = path
                        changed = True

    def qualified(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.bound.get(node.id)
        if isinstance(node, ast.Attribute):
            owner = self.qualified(node.value)
            if owner is None:
                return None
            if owner.startswith(_FLEET):
                return None if self.fleet is None else self.fleet.attribute(owner[len(_FLEET) :], node.attr)
            return f"{owner}.{node.attr}"
        return None


def _is_plain_alias(node: ast.Assign | ast.AnnAssign) -> bool:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return all(isinstance(target, ast.Name) for target in targets)


class _ReaderReferences(ast.NodeVisitor):
    """Each read of Arrow IPC in a module, with the qualified name of the function around it."""

    def __init__(self, tree: ast.Module, *, fleet: _Fleet | None = None, module: str = "", is_package: bool = False) -> None:
        self._bindings = _Bindings(fleet, module, is_package, tree.body)
        self._constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.found: list[tuple[str, ast.AST]] = []
        self._scope: list[str] = []
        self._dereferenced: set[int] = set()
        self._aliased: set[int] = set()
        self.visit(tree)

    def _qualified(self, node: ast.expr) -> str | None:
        return self._bindings.qualified(node)

    def _record(self, node: ast.AST) -> None:
        self.found.append((".".join(self._scope) or "<module>", node))

    def _scoped(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_arguments(node.args)
        self._scope.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self._scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _scoped

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expr in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(expr)
        self._scope.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self._scope.pop()

    def _visit_arguments(self, args: ast.arguments) -> None:
        # Annotations name types and read nothing; defaults are values and are visited.
        for default in [*args.defaults, *(d for d in args.kw_defaults if d is not None)]:
            self.visit(default)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_arguments(node.args)
        self.visit(node.body)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            if _is_plain_alias(node):
                self._aliased.add(id(node.value))
            self.visit(node.value)
        self.visit(node.target)

    def visit_Assign(self, node: ast.Assign) -> None:
        if _is_plain_alias(node):
            self._aliased.add(id(node.value))
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        # What is imported under `if TYPE_CHECKING:` never runs; the names it binds are still followed.
        test = node.test
        if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"):
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = _absolute(node.module, node.level, self._bindings.importer, self._bindings.is_package)
        if module is None:
            return
        if any(alias.name == "*" for alias in node.names):
            if _is_reader_module(module) or _is_reader(module):
                self._record(node)
            return
        # Binding a reader by name is a read where it is bound: a module that only re-exports one is refused.
        if any(_is_reader(self._bindings.bound.get(alias.asname or alias.name)) for alias in node.names):
            self._record(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._dereferenced.add(id(node.value))
        self._reference(node)
        self.visit(node.value)

    def visit_Name(self, node: ast.Name) -> None:
        self._reference(node)

    def _reference(self, node: ast.Name | ast.Attribute) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        path = self._qualified(node)
        escapes = _is_reader_module(path) and id(node) not in self._dereferenced and id(node) not in self._aliased
        if _is_reader(path) or escapes:
            self._record(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.value, ast.Attribute) and node.value.attr == "modules" and self._names_a_reader_module(node.slice):
            self._record(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = self._call_name(node.func)
        if func in {"isinstance", "issubclass"} and node.args:
            # The class a value is checked against is not read from.
            self.visit(node.args[0])
            return
        if func in _DYNAMIC_IMPORTS and node.args and self._names_a_reader_module(node.args[0]):
            self._record(node)
        if self._qualified(node.func) == _DATASET and self._reads_an_ipc_format(node):
            self._record(node)
        self.generic_visit(node)

    def _call_name(self, func: ast.expr) -> str | None:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute) and func.attr == "import_module":
            return "importlib.import_module"
        return None

    def _names_a_reader_module(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Constant) and isinstance(node.value, str) and _is_tracked(node.value)

    def _reads_an_ipc_format(self, call: ast.Call) -> bool:
        """`dataset(source, schema, format, ...)`: absent is Parquet; a format the scan cannot name may be IPC."""
        formats = [keyword.value for keyword in call.keywords if keyword.arg == "format"] + call.args[2:3]
        if not formats:
            return False
        fmt = formats[0]
        if isinstance(fmt, ast.Name) and fmt.id in self._constants:
            return self._constants[fmt.id] in _IPC_FORMATS
        if isinstance(fmt, ast.Constant) and isinstance(fmt.value, str):
            return fmt.value in _IPC_FORMATS
        return not (isinstance(fmt, ast.Call) and self._qualified(fmt.func) in _OTHER_FORMAT_CLASSES)


def _references(source: str) -> list[str]:
    """The scopes a single standalone module reads Arrow IPC in."""
    return [scope for scope, _ in _ReaderReferences(ast.parse(source)).found]


def _scan(sources: dict[Path, str]) -> dict[Path, list[str]]:
    fleet = _Fleet(sources)
    found = {
        path: [scope for scope, _ in _ReaderReferences(fleet.trees[path], fleet=fleet, module=fleet.names[path], is_package=fleet.packages[path]).found]
        for path in sources
    }
    return {path: scopes for path, scopes in found.items() if scopes}


def _fleet_sources() -> dict[Path, str]:
    paths = sorted(p.relative_to(_REPO_ROOT) for pattern in ("services/*/src/**/*.py", "packages/*/src/**/*.py") for p in _REPO_ROOT.glob(pattern))
    return {path: (_REPO_ROOT / path).read_text() for path in paths}


@cache
def _fleet_scan() -> dict[Path, list[str]]:
    return _scan(_fleet_sources())


def test_no_module_decodes_arrow_ipc_outside_the_validating_decoder() -> None:
    offenders = {str(path): scopes for path, scopes in _fleet_scan().items() if path != _CANONICAL}
    assert not offenders, f"decode through service_kit.lancekit.arrow_ipc.decode_arrow_stream / decode_arrow_stream_or_file: {offenders}"


def test_the_scan_reads_the_fleet() -> None:
    """Without this, a glob that matched nothing would pass the gate above."""
    assert _CANONICAL in _fleet_scan(), "the scan found no read in the canonical decoder — it is not reading the fleet"


def test_every_read_in_the_decoder_passes_through_the_full_validation() -> None:
    # One tree, so the nodes the visitor recorded are the same objects found inside `_validated(...)`.
    tree = ast.parse((_REPO_ROOT / _CANONICAL).read_text())
    module = _ReaderReferences(tree)
    validated = {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_validated"
        for inner in ast.walk(node)
    }
    assert module.found, "the canonical decoder reads no Arrow IPC — the gate is looking at the wrong module"
    unvalidated = [f"{scope}:{getattr(node, 'lineno', '?')}" for scope, node in module.found if id(node) not in validated]
    assert not unvalidated, f"a read in the decoder does not pass through _validated: {unvalidated}"


def test_the_decoders_validation_is_full() -> None:
    tree = ast.parse((_REPO_ROOT / _CANONICAL).read_text())
    [validated] = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_validated"]
    full = [
        node
        for node in ast.walk(validated)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "validate"
        and any(k.arg == "full" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords)
    ]
    assert full, "_validated must call validate(full=True): validate() alone accepts decreasing offsets and non-UTF-8 values"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import pyarrow as pa\ndef f(b):\n    return pa.ipc.open_stream(b).read_all()\n", id="pa.ipc"),
        pytest.param("import pyarrow.ipc as paipc\ndef f(b):\n    return paipc.open_file(b).read_all()\n", id="aliased-ipc-module"),
        pytest.param("from pyarrow import ipc\ndef f(b):\n    return ipc.RecordBatchStreamReader(b).read_all()\n", id="from-pyarrow-import-ipc"),
        pytest.param("import pyarrow\ndef f(b):\n    return pyarrow.ipc.open_stream(b).read_all()\n", id="pyarrow.ipc"),
        pytest.param("import pyarrow.ipc\ndef f(b):\n    return pyarrow.ipc.open_stream(b).read_all()\n", id="import-pyarrow.ipc"),
        pytest.param("import pyarrow as pa\ndef f(b):\n    return pa.RecordBatchFileReader(b).read_all()\n", id="top-level-reader"),
        pytest.param("import pyarrow as pa\nipc = pa.ipc\ndef f(b):\n    return ipc.open_stream(b).read_all()\n", id="ipc-module-assigned-to-a-name"),
        pytest.param("import pyarrow as pa\ndef f(b):\n    return pa.ipc.MessageReader.open_stream(b).read_next_message()\n", id="a-classmethod-of-a-reader"),
        pytest.param(
            "import functools\nimport pyarrow as pa\ndef f(b):\n    return functools.partial(pa.ipc.open_stream)(b).read_all()\n",
            id="a-reader-passed-as-a-value",
        ),
        pytest.param("from pyarrow import lib\ndef f(b):\n    return lib.read_message(b)\n", id="pyarrow.lib"),
        pytest.param(
            "import pyarrow as pa\nimport pyarrow.feather as feather\ndef f(b):\n    return feather.read_table(pa.BufferReader(b))\n", id="feather.read_table"
        ),
        pytest.param("from pyarrow import feather\ndef f(b):\n    return feather.read_feather(b)\n", id="from-pyarrow-import-feather"),
        pytest.param("import pyarrow.dataset as ds\ndef f(b):\n    return ds.dataset(b, format='arrow').to_table()\n", id="dataset-with-an-arrow-format"),
        pytest.param(
            "import pyarrow.dataset as ds\ndef f(b, fmt):\n    return ds.dataset(b, format=fmt).to_table()\n", id="dataset-with-a-format-it-cannot-name"
        ),
        pytest.param("import pyarrow.dataset as ds\ndef f(b):\n    return ds.dataset(b, None, 'ipc').to_table()\n", id="dataset-with-a-positional-ipc-format"),
        pytest.param(
            "import pyarrow.dataset as ds\nIPC = 'arrow'\ndef f(b):\n    return ds.dataset(b, format=IPC).to_table()\n", id="dataset-with-an-ipc-constant"
        ),
        pytest.param("import pyarrow.dataset as ds\ndef f(path):\n    return ds.IpcFileFormat().make_fragment(path)\n", id="ipc-file-format"),
        pytest.param("from pyarrow._feather import FeatherReader\ndef f(b):\n    return FeatherReader(b).read()\n", id="private-feather-reader"),
        pytest.param(
            "import pyarrow.feather as feather\ndef f(b):\n    return feather._feather.FeatherReader(b).read()\n",
            id="private-feather-through-the-public-module",
        ),
        pytest.param("import pyarrow._dataset as _ds\ndef f(b):\n    return _ds.IpcFileFormat().make_fragment(b)\n", id="private-dataset-module"),
        pytest.param("import pyarrow.flight as flight\nclass Put(flight.FlightServerBase):\n    def f(self):\n        pass\n", id="a-flight-server"),
        pytest.param("import pandas as pd\ndef f(b):\n    return pd.read_feather(b)\n", id="pandas-read-feather"),
        pytest.param("from lancedb.namespace import _arrow_ipc_to_record_batch_reader as r\ndef f(b):\n    return r(b)\n", id="lancedb-ipc-helper"),
        pytest.param(
            "import pyarrow\ndef _pyarrow():\n    return pyarrow\ndef f(b):\n    return _pyarrow().ipc.open_stream(b)\n",
            id="a-lazy-import-helper-returns-the-module",
        ),
        pytest.param("def f(b):\n    import pyarrow as pa\n    return pa\n", id="a-function-local-import-returned"),
        pytest.param("import pyarrow as pa\nclass H:\n    def __init__(self):\n        self.ipc = pa.ipc\n", id="a-module-stored-on-an-object"),
        pytest.param("import pyarrow as pa\ndef f(b, ipc=pa.ipc):\n    return ipc.open_stream(b)\n", id="a-module-as-a-default"),
        pytest.param("import pyarrow as pa\ndef f(b):\n    ipc, _ = pa.ipc, 1\n    return ipc\n", id="a-module-in-a-tuple"),
        pytest.param("import pyarrow as pa\ndef f(b):\n    for ipc in (pa.ipc,):\n        return ipc\n", id="a-module-in-a-loop"),
        pytest.param("import pyarrow as pa\ndef f(b):\n    return getattr(pa.ipc, 'open_' + 'stream')(b)\n", id="getattr-on-a-module"),
        pytest.param("import importlib\ndef f(b):\n    return importlib.import_module('pyarrow.ipc').open_stream(b)\n", id="importlib-by-literal"),
        pytest.param("def f(b):\n    return __import__('pyarrow').ipc.open_stream(b)\n", id="dunder-import-by-literal"),
        pytest.param("import sys\ndef f(b):\n    return sys.modules['pyarrow.ipc'].open_stream(b)\n", id="sys-modules-by-literal"),
    ],
)
def test_the_scan_sees_every_spelling_of_a_read(source: str) -> None:
    """The gate is only as good as its resolver: each spelling below reads (or can read) Arrow IPC."""
    assert _references(source), "the scan does not see this read"


def test_a_reader_imported_by_name_is_a_read_at_the_import() -> None:
    assert _references("from pyarrow.ipc import open_stream as o\ndef f(b):\n    return o(b).read_all()\n") == ["<module>", "f"]


def test_a_star_import_from_a_reader_module_is_a_read_the_scan_cannot_resolve() -> None:
    assert _references("from pyarrow.ipc import *\ndef f(b):\n    return open_stream(b).read_all()\n") == ["<module>"]


def test_a_reader_re_exported_by_a_fleet_module_is_refused_on_both_sides() -> None:
    compat = Path("packages/kit/src/kit/_compat.py")
    consumer = Path("services/app/src/app/use.py")
    found = _scan(
        {
            compat: "from pyarrow.ipc import open_stream as open_stream\n",
            consumer: "from kit._compat import open_stream\ndef f(b):\n    return open_stream(b).read_all()\n",
        }
    )
    assert found == {compat: ["<module>"], consumer: ["<module>", "f"]}


def test_pyarrow_reached_through_a_fleet_module_is_followed() -> None:
    reader = Path("packages/kit/src/kit/reader.py")
    consumer = Path("services/app/src/app/use.py")
    found = _scan(
        {
            reader: "import pyarrow as pa\ndef g(t):\n    return pa.Table\n",
            Path("packages/kit/src/kit/__init__.py"): "",
            consumer: "from kit.reader import pa as via\nimport kit.reader\ndef f(b):\n    return via.ipc.open_stream(b)\ndef h(b):\n    return kit.reader.pa.ipc.open_file(b)\n",
        }
    )
    assert found == {consumer: ["f", "h"]}


def test_a_relative_re_export_is_followed() -> None:
    found = _scan(
        {
            Path("packages/kit/src/kit/__init__.py"): "from .compat import pa\n",
            Path("packages/kit/src/kit/compat.py"): "import pyarrow as pa\n",
            Path("services/app/src/app/use.py"): "from kit import pa\ndef f(b):\n    return pa.ipc.open_stream(b)\n",
        }
    )
    assert found == {Path("services/app/src/app/use.py"): ["f"]}


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("def f(read_schema, fs):\n    fs.open_file('x')\n    return read_schema('uri', 1)\n", id="a-name-that-only-looks-like-a-reader"),
        pytest.param(
            "import pyarrow as pa\ndef f(t):\n    sink = pa.BufferOutputStream()\n    with pa.ipc.new_stream(sink, t.schema) as w:\n        w.write_table(t)\n",
            id="a-writer",
        ),
        pytest.param("import pyarrow.dataset as ds\ndef f(path):\n    return ds.dataset(path).to_table()\n", id="dataset-with-its-default-format"),
        pytest.param(
            "import pyarrow.dataset as ds\ndef f(path):\n    return ds.dataset(path, format='parquet').to_table()\n", id="dataset-with-a-parquet-format"
        ),
        pytest.param(
            "import pyarrow.dataset as ds\ndef f(path):\n    return ds.dataset(path, format=ds.ParquetFileFormat()).to_table()\n",
            id="dataset-with-a-parquet-format-object",
        ),
        pytest.param(
            "import pyarrow.dataset as ds\nPARQUET = 'parquet'\ndef f(path):\n    return ds.dataset(path, format=PARQUET).to_table()\n",
            id="dataset-with-a-parquet-constant",
        ),
        pytest.param(
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from pyarrow.ipc import RecordBatchStreamReader\ndef f(r: RecordBatchStreamReader) -> RecordBatchStreamReader:\n    x: RecordBatchStreamReader = r\n    return x\n",
            id="a-reader-named-only-in-annotations",
        ),
        pytest.param("import pyarrow as pa\ndef f(r):\n    return isinstance(r, pa.ipc.RecordBatchStreamReader)\n", id="an-isinstance-check"),
        pytest.param("import pyarrow as pa\ndef f(t: pa.Table) -> pa.Schema:\n    return pa.table({'a': [1]}).schema\n", id="the-module-dereferenced"),
    ],
)
def test_the_scan_ignores_what_reads_no_arrow_ipc(source: str) -> None:
    assert _references(source) == []
