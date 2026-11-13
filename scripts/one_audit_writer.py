"""The compliance trail has one writer: fail when lakehouse code names the `lance.audit` logger outside its writer.

The Collector routes a log record to the audit trail by its instrumentation scope, which the OpenTelemetry SDK's
`LoggingHandler` names after the Python logger that made the record (`chart/templates/otel-collector.yaml`,
`filter/audit_only` and `filter/drop_audit`). Every record on the `lance.audit` logger is therefore an audit record,
and `audit()` is what gives one its closed vocabulary, its format version and its request id, so no other module may
obtain that logger.

Reaching it through `lakehouse.governed.audit` (`AUDIT_LOGGER`, `_log`, imported or read as an attribute) is a ruff
TID251 ban in `pyproject.toml`. What ruff cannot express is the logger's name written as a string, so this lint, run in
`make lint` and `dagger call lint`, refuses that in the lakehouse's production code (each member's `src`):
`"lance.audit"` or a child of it, and `getChild("audit")`, the one-step route to it from the `lance` logger. A name
assembled at run time (`"lance." + part`) is not seen: the rule is a floor under review, not a proof. The suites are
not judged: a test captures the trail, and nothing it logs ships.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

from _files_under import files_under


ROOT = Path(__file__).resolve().parent.parent

#: The one module that writes the trail.
WRITER = "packages/lakehouse/src/lakehouse/governed/audit.py"
#: The logger's name, as `audit.AUDIT_LOGGER` holds it, and its last segment.
LOGGER = "lance.audit"
LEAF = LOGGER.rpartition(".")[2]
#: The lakehouse's production trees.
SOURCE_TREES = ("packages/lakehouse/src", "services/catalog/src", "services/lineage/src", "services/maintenance/src")


def names_the_logger(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """Each line of `tree` that names the trail's logger as a string, with what it named."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and (node.value == LOGGER or node.value.startswith(f"{LOGGER}.")):
            yield node.lineno, repr(node.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getChild"
            and any(isinstance(arg, ast.Constant) and arg.value == LEAF for arg in node.args)
        ):
            yield node.lineno, f"getChild({LEAF!r})"


def main(argv: Sequence[str] | None = None) -> int:
    """Print every place a module other than the writer names the trail's logger, and answer 1 when there is one."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository to judge")
    args = parser.parse_args(argv)
    hits = [
        f"{name}:{line}: {what}"
        for name in files_under(args.root, SOURCE_TREES)
        if name.endswith(".py") and name != WRITER
        for line, what in names_the_logger(ast.parse((args.root / name).read_text(encoding="utf-8"), filename=name))
    ]
    if hits:
        print(f"!! only {WRITER} names the `{LOGGER}` logger; these modules name it too:", *hits, sep="\n  ", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
