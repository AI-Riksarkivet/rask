"""The code names no reference product: fail when a code file names a product rask's design research reads.

Owner, 2026-11-07: "I don't like you mentioning lakekeeper in code". rask's code states its own rationale; naming
another product in it reads as the code being a copy of that product. The comparison belongs where research is
recorded, so `docs/`, the ADRs, the agent instructions and issue bodies keep it, and every code tree here is held to
the rule: a comment, a docstring, a test, the chart, a script. A rule about what files say is a lint, so this runs in
`make lint` and `dagger call lint`.

`REFERENCE_PRODUCTS` is the one place in code that names one, as the no-client lint's `DEPARTED` is for clients.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from _files_under import files_under


ROOT = Path(__file__).resolve().parent.parent

#: The products the design research reads and the code never names, matched in any case.
REFERENCE_PRODUCTS = frozenset({"lakekeeper"})
#: The code trees, and the root files that are code.
CODE_TREES = ("packages", "services", "tests", "chart", "deploy", "scripts", "frontend", ".dagger", ".docker", ".github")
CODE_FILES = ("conftest.py", "Makefile", "pyproject.toml", "prek.toml", ".importlinter", ".vulture-whitelist.py")
#: Vendored upstream files rask does not write, this lint, and its own test, which plants the name to show it refused.
EXCLUDED = (
    "chart/crds-bootstrap/",
    "/charts/",
    "scripts/code_names_no_reference_product.py",
    "tests/unit/test_the_code_names_no_reference_product.py",
)


def pattern(names: frozenset[str]) -> re.Pattern[str]:
    """One case-insensitive alternation over the names."""
    return re.compile("|".join(re.escape(name) for name in sorted(names)), re.IGNORECASE)


def code_files(root: Path) -> list[str]:
    """The files under the code trees (`_files_under.files_under`), less the vendored ones, this lint and its test."""
    return [name for name in files_under(root, [*CODE_TREES, *CODE_FILES]) if not any(excluded in name for excluded in EXCLUDED)]


def main(argv: Sequence[str] | None = None) -> int:
    """Print every place a code file names a reference product, and answer 1 when there is one."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository to judge")
    args = parser.parse_args(argv)
    found = pattern(REFERENCE_PRODUCTS)
    hits: list[str] = []
    for name in code_files(args.root):
        try:
            text = (args.root / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hits += [f"{name}:{n}: {m.group(0)!r}" for n, line in enumerate(text.splitlines(), 1) for m in found.finditer(line)]
    for hit in hits:
        print(hit)
    if hits:
        print(
            f"{len(hits)} place(s) where code names a reference product; write the rationale itself and keep the comparison in docs/ or an ADR",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
