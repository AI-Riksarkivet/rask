"""A `dagger call` must not snapshot a directory another process is still writing to.

Measured 2026-08-26, building the seven zone images while a sibling session ran pytest::

    ✘ filesync ERROR
    ! failed to sync: conflict at ".pytest_cache/v/cache/nodeids":
      mod time changed from "1787724886594616499" to "1787724918931758389" during sync

Three of seven zone builds died that way and four survived, decided purely by which ones happened to
be syncing while `pytest` rewrote its cache. Nothing about the images differed. That is the worst
shape a build failure can take: non-deterministic, unattributable, and indistinguishable from a real
defect in the zone being built — the operator reads "web-compute failed to build" and goes looking at
web-compute.

The cause is that `+ignore` lists what leaves the host, and a tool cache that no image ever needs was
not on it. `.pytest_cache`, `__pycache__` and `.ruff_cache` are all written by ordinary commands a
developer runs *while* a build is in flight (`make test`, `uvx ruff check`, any editor's test runner),
so the race is not rare — it is whatever the machine happens to be doing.

THE SECOND HALF IS THE ESTATE'S OWN RECORDED LESSON, HALF-APPLIED. Dagger's `+ignore` matches
root-relative unless the pattern is `**/`-prefixed. `test_dagger_context_is_hermetic.py` already
writes this down — "the same mistake was already made with `+ignore` and bare `.venv`/`node_modules`:
a root-relative pattern leaves `runners/*/.venv` in the context" — and `.dagger/scan.go` was duly
fixed to `**/.venv` / `**/node_modules`. `.dagger/images.go` was not, and still carried bare `.venv`,
bare `node_modules`, and a hand-patched `frontend/node_modules` that is itself the fingerprint of
someone hitting the root-only limit and fixing exactly one instance of it. So the functions that BUILD
every image shipped the weaker list, and the ones that merely SCAN shipped the stronger one.

Why a test rather than a comment: `+ignore` is a Dagger pragma read out of comment text, so nothing in
the Go toolchain type-checks it, no linter sees it, and a copy-pasted function silently inherits
whatever list it was copied from — which is precisely how three identical stale copies came to exist
in one file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
_DAGGER = REPO_ROOT / ".dagger"

#: Tool caches a build never needs and a concurrent command rewrites mid-sync.
_MUST_EXCLUDE = ("**/.pytest_cache", "**/__pycache__", "**/.ruff_cache")

#: Directories that nest, so a root-relative pattern misses copies. `runners/*/.venv` is the live case.
_MUST_BE_RECURSIVE = (".venv", "node_modules")


def _ignore_pragmas() -> list[tuple[str, int, list[str]]]:
    """(file, line, patterns) for every `+ignore` pragma in the Dagger module.

    The pragma wraps across comment lines, so the body is rejoined before the string literals are
    pulled out — reading only the first line would silently see a third of each list.
    """
    found: list[tuple[str, int, list[str]]] = []
    for path in sorted(_DAGGER.glob("*.go")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"//\s*\+ignore=\[(.*?)\]", text, re.DOTALL):
            body = " ".join(line.strip().lstrip("/").strip() for line in match.group(1).split("\n"))
            line_no = text[: match.start()].count("\n") + 1
            found.append((path.name, line_no, re.findall(r'"([^"]+)"', body)))
    return found


def test_the_scan_actually_finds_the_pragmas() -> None:
    """Non-vacuity: a regex that stopped matching would pass every assertion below in silence."""
    pragmas = _ignore_pragmas()
    assert len(pragmas) >= 6, f"only {len(pragmas)} +ignore pragmas found; the module defines at least six"
    assert {name for name, _, _ in pragmas} >= {"images.go", "scan.go"}, "both files define build contexts and the scan no longer sees both"


@pytest.mark.parametrize("cache", _MUST_EXCLUDE)
def test_every_build_context_excludes_the_tool_cache_that_races_the_sync(cache: str) -> None:
    offenders = [f"{name}:{line}" for name, line, patterns in _ignore_pragmas() if cache not in patterns]
    assert not offenders, (
        f"{cache} is not excluded from these Dagger build contexts, so a `dagger call` racing an "
        f"ordinary test or lint run dies with `failed to sync: conflict at ...`:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("directory", _MUST_BE_RECURSIVE)
def test_nesting_directories_are_excluded_recursively(directory: str) -> None:
    """A bare `node_modules` leaves every nested copy in the context; `**/node_modules` does not."""
    offenders: list[str] = []
    for name, line, patterns in _ignore_pragmas():
        root_relative = [p for p in patterns if p == directory or (p.endswith("/" + directory) and not p.startswith("**/"))]
        if root_relative:
            offenders.append(f"{name}:{line} carries {root_relative}")
    assert not offenders, (
        f"these +ignore lists match `{directory}` only at the repo root, so nested copies "
        f"(runners/*/.venv, a package's own node_modules) still enter the build context:\n  " + "\n  ".join(offenders)
    )
