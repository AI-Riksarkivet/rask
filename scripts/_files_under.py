"""The files a lint over the repository's own text reads, one listing for every such lint.

`scripts/lakehouse_names_no_client.py` and `scripts/code_names_no_reference_product.py` judge the same kind of tree
the same way: the tracked files and the untracked ones git does not ignore, or, in a copy of the source with no `.git`
(a CI container's, `dagger call lint`), every file under the scope less what a checkout carries and git ignores. One
listing, so two lints cannot judge two different trees.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path


#: The directories a copy of the source with no `.git` carries and git ignores: environments, caches, build output.
UNTRACKED_PARTS = frozenset({".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".svelte-kit", "dist", "build"})


def files_under(root: Path, scope: Sequence[str]) -> list[str]:
    """The files under each path in `scope`, relative to `root`."""
    if (root / ".git").exists():
        listed = subprocess.run(  # noqa: S603
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", *scope],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return listed.splitlines()
    return sorted(
        str(p.relative_to(root))
        for entry in scope
        for p in ([root / entry] if (root / entry).is_file() else (root / entry).rglob("*"))
        if p.is_file() and not UNTRACKED_PARTS.intersection(p.relative_to(root).parts)
    )
