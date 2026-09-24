"""The repository's files, for the gates that scan the whole tree — with or without git.

The repo-shape gates (`test_no_docker`, the locator sweep, the auth-name sweep, the producer-URI
sweep) enumerate every file and grep it. Each shelled out to `git ls-files`, which is exact and, in
the container CI runs them in, absent: `ms-test` fails with `FileNotFoundError: [Errno 2] No such
file or directory: 'git'`, and has done for days. A gate that cannot RUN is worse than one that
cannot fail — it reports nothing at all, and the job around it goes red for a reason no reader
connects to the rule being enforced.

`.dagger`'s base container excludes `.git` deliberately (the build cache would bust on every commit),
so the answer is not to ship the history into CI. It is that a repo-shape gate should read the source
tree it was handed and not require a version-control system to do it.

WHERE THE FALLBACK RUNS, THE TREE IS ALREADY CLEAN. CI checks out fresh and `.dagger/main.go:76`
hands the container everything but `**/.env`, `.venv`, `.git` and `node_modules` — so the source
directory there is the tracked files and little else, and the walk's answer is git's answer.

ON A DEVELOPER'S TREE IT IS A SUPERSET, and that is why git goes first. Measured on this host: 5,813
walked against 3,832 tracked, zero tracked files missing; the extra 1,981 are local residue —
`.coverage`, generated Dagger bindings, Playwright logs, the soak CSV. A gate handed that set would
report violations in junk. It never is: git answers wherever git exists.

GIT FIRST WHERE IT EXISTS, so a developer's run is byte-identical to what it has always been and the
fallback is exercised only where it is needed. `test_the_fallback_walk_loses_no_tracked_file` pins the
superset property on any host that has both.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


#: Directories a repo-shape gate must never descend into: build output, caches, vendored trees and
#: the soak record. Named rather than derived from `.gitignore`, because parsing that file correctly
#: is its own project and the gates only need the coarse answer.
_SKIP = frozenset(
    {
        ".git",
        ".cache",
        ".localbin",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".soak",
        ".svelte-kit",
        ".turbo",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
        "node_modules",
        "target",
    }
)


def git_tracked_files(root: Path) -> list[str] | None:
    """Every path `git ls-files` reports, or ``None`` when git cannot answer.

    ``None`` covers both shapes of absence with one return, because the caller's response is the same:
    no git binary (the CI container) and no repository (an exported source tree).
    """
    try:
        done = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return [name for name in done.stdout.split("\0") if name]


def walked_files(root: Path) -> list[str]:
    """Every file under ``root``, skipping build output, caches and vendored trees."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP]
        for name in filenames:
            found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return found


def repo_files(root: Path) -> list[str]:
    """The files a repo-shape gate should scan: git's answer where there is one, the walk otherwise."""
    return git_tracked_files(root) or walked_files(root)
