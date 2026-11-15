"""The lakehouse runs no Dapr: fail when a code file names it outside the guards that keep it out ([[XC-199]], ADR 0207).

No lakehouse pod runs a sidecar and no lakehouse package imports `dapr`; ruff (`TID251`), import-linter and the chart's
render test hold those two. What they do not see is the rest of the code: a fixture chart, a Dagger lane, a script or
the Makefile could install, annotate or call one. A rule about what files say is a lint, so this runs in `make lint` and
`dagger call lint`.

The guards themselves name it; `GUARDS` lists those files. The research and the decisions (`docs/`, the ADRs) and
the agent instructions are not code and are not judged.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from _files_under import files_under


ROOT = Path(__file__).resolve().parent.parent

#: The name, matched in any case.
NAME = re.compile("dapr", re.IGNORECASE)
#: This lint's own name, which the lines that run it carry (the Makefile's `lint`, the Dagger lint lane) and which is no
#: mention of the framework.
OWN_NAME = "the_lakehouse_runs_no_dapr"
#: The code trees, and the root files that are code.
CODE_TREES = ("packages", "services", "tests", "chart", "deploy", "scripts", "frontend", ".dagger", ".docker", ".github")
CODE_FILES = ("conftest.py", "Makefile", "pyproject.toml", "prek.toml", ".importlinter", ".vulture-whitelist.py")
#: The files that name it to keep it out: the ruff ban, the import contract, the render test, this lint and its own test.
GUARDS = frozenset(
    {
        "pyproject.toml",
        ".importlinter",
        "tests/unit/test_invariants.py",
        "scripts/the_lakehouse_runs_no_dapr.py",
        "tests/unit/test_the_lakehouse_runs_no_dapr.py",
    }
)
#: Vendored upstream files rask does not write.
VENDORED = ("chart/crds-bootstrap/", "/charts/")


def code_files(root: Path) -> list[str]:
    """The files under the code trees (`_files_under.files_under`), less the vendored ones and the guards."""
    return [name for name in files_under(root, [*CODE_TREES, *CODE_FILES]) if name not in GUARDS and not any(part in name for part in VENDORED)]


def main(argv: Sequence[str] | None = None) -> int:
    """Print every place a code file names Dapr, and answer 1 when there is one."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository to judge")
    args = parser.parse_args(argv)
    hits: list[str] = []
    for name in code_files(args.root):
        try:
            text = (args.root / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hits += [f"{name}:{n}: {m.group(0)!r}" for n, line in enumerate(text.splitlines(), 1) for m in NAME.finditer(line.replace(OWN_NAME, ""))]
    for hit in hits:
        print(hit)
    if hits:
        print(
            f"{len(hits)} place(s) where code names Dapr: the lakehouse runs none (ADR 0207, the rask-dapr skill names where each need is met)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
