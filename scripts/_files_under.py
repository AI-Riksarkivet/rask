"""The files a lint over the repository's own text reads, one listing for every such lint.

`scripts/lakehouse_names_no_client.py` and `scripts/code_names_no_reference_product.py` judge the same kind of tree
the same way: the tracked files and the untracked ones git does not ignore, or, in a copy of the source with no `.git`
(a CI container's, `dagger call lint`), every file under the scope less what a checkout carries and git ignores. One
listing, so two lints cannot judge two different trees.

Without `.git` a file's tracked state is unknown, so only what no source tree holds is left out: environments and caches
by name at any depth, and build output only where the builds write it. A tracked directory that happens to be called
`build` or `dist` elsewhere is judged.
"""

import re
import subprocess
from collections.abc import Sequence
from pathlib import Path


#: The directories a copy of the source with no `.git` carries and git ignores at any depth: environments and caches.
UNTRACKED_PARTS = frozenset({".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".svelte-kit"})
#: Where a build writes its output: `uv build` at the root or in a workspace member, the console's static build.
BUILD_OUTPUT = re.compile(r"(?:(?:packages|services)/[^/]+/|frontend/console/)?(?:dist|build)/")


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
        if p.is_file() and not _untracked(p.relative_to(root))
    )


def _untracked(relative: Path) -> bool:
    """Whether a file of a copy with no `.git` is an environment's, a cache's or a build's, which no checkout tracks."""
    return bool(UNTRACKED_PARTS.intersection(relative.parts)) or BUILD_OUTPUT.match(relative.as_posix()) is not None
