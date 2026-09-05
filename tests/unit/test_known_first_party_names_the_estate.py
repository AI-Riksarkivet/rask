"""ruff's `known-first-party` is enumerated, so it drifts when the estate changes shape.

The list decides which isort BLOCK an import lands in: a name missing from it makes ruff sort
`from catalog.core.config import ...` beside `import httpx`, and a name that outlives its package
keeps reserving first-party status for a module nobody can import. The first half was measured — the
list carried nine of nineteen names for months, and correcting it cost a 465-diagnostic repo-wide
re-sort (`docs/DECISIONS.md "The Python estate audit"` X3). The second half is the deletion direction, and nothing watched it:
`packages/tracker` could be removed from the workspace, the lock and the testpaths and still be
declared first-party here, because no gate compared this list to disk.

The sibling gates own the other enumerated lists — `test_coverage_denominator` the coverage source,
`test_invariants.test_every_workspace_test_directory_is_in_the_root_testpaths` the testpaths. This is
the same shape for the isort list: derive the expectation from the same globs `[tool.uv.workspace]
members` uses, and compare in both directions.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _configured_first_party() -> list[str]:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["ruff"]["lint"]["isort"]["known-first-party"]


def _import_names_on_disk() -> list[str]:
    """Every workspace member's importable top-level package, from the workspace's own globs.

    `packages/service-kit` ships `service_kit`, so the name is read off the src tree rather than
    derived from the directory. A deps-only member (`ray-cluster-env`) ships no `src/`, contributes
    no import name, and correctly appears in neither side of this comparison.
    """
    found = {
        path.name
        for pattern in ("packages/*/src/*", "services/*/src/*")
        for path in REPO_ROOT.glob(pattern)
        if path.is_dir() and not path.name.endswith(".egg-info")
    }
    return sorted(found)


def test_known_first_party_names_every_workspace_import_and_nothing_else() -> None:
    configured, on_disk = sorted(_configured_first_party()), _import_names_on_disk()

    assert on_disk, "no packages/*/src or services/*/src package found — the glob is broken, not the estate"
    assert configured == on_disk, (
        "ruff's known-first-party list and the workspace disagree.\n"
        f"  importable but NOT declared first-party: {sorted(set(on_disk) - set(configured))}\n"
        f"  declared first-party but NOT importable: {sorted(set(configured) - set(on_disk))}\n"
        "A missing name sorts that module into the third-party block; a surviving name reserves "
        "first-party status for a package that no longer exists. Edit [tool.ruff.lint.isort] "
        "known-first-party in the same change that adds or deletes the member."
    )
