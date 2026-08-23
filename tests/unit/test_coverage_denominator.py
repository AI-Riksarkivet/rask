"""The coverage denominator must name every src root, or the percentage is a tautology.

`[tool.coverage.run] source` used to be `["packages/", "services/"]` — two paths that are not
importable package roots, because every workspace member is a src-layout project. coverage.py's
unexecuted-file discovery therefore pruned the tree, and the denominator collapsed to "the files some
test happened to import". Measured on a `packages/tracker/tests` run: **3 files discovered before, 427
after**. A percentage computed over only the files a run touched cannot go down when coverage gets
worse; it is not a measurement.

coverage.py does not glob `source`, so the list has to be enumerated — and an enumerated list is
exactly the thing that drifts when a package is added. `rask-architecture` records the same hazard one
layer up: workspace membership IS globbed, so "a new Python library/service is picked up by the glob"
and nothing else needs editing. That asymmetry is the bug waiting to happen — the member joins the
workspace silently and falls out of the coverage denominator silently — so this gate derives the
expected list from the same globs the workspace uses and compares.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _configured_source() -> list[str]:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["coverage"]["run"]["source"]


def _src_roots_on_disk() -> list[str]:
    """Every workspace member's src root, from the same globs `[tool.uv.workspace] members` uses."""
    roots = [p for pattern in ("packages/*/src", "services/*/src") for p in REPO_ROOT.glob(pattern) if p.is_dir()]
    return sorted(p.relative_to(REPO_ROOT).as_posix() for p in roots)


def test_the_denominator_names_every_workspace_src_root() -> None:
    configured, on_disk = sorted(_configured_source()), _src_roots_on_disk()

    assert on_disk, "no packages/*/src or services/*/src found — the glob is broken, not the estate"
    assert configured == on_disk, (
        "the coverage denominator and the workspace disagree.\n"
        f"  in the workspace but NOT measured: {sorted(set(on_disk) - set(configured))}\n"
        f"  measured but NOT in the workspace: {sorted(set(configured) - set(on_disk))}\n"
        "Workspace membership is globbed, so a new member joins silently; the coverage source is "
        "enumerated because coverage.py cannot glob it. Add the src root to [tool.coverage.run] source."
    )


def test_no_omit_row_deletes_real_implementation() -> None:
    """`**/__init__.py` deleted the estate's front door from its own report.

    409 statements across 69 files, including the whole gateway service — route table, the
    `_CLIENT_SPOOFABLE` header strip, the proxy — because a src-layout service's implementation may
    legitimately live in `__init__.py`. An empty package marker contributes 0 statements, so omitting
    them bought nothing and cost the one service that most needed measuring.
    """
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    omit = config["tool"]["coverage"]["run"]["omit"]

    assert "**/__init__.py" not in omit, (
        "`**/__init__.py` is back in the coverage omit list. It was added for empty package markers "
        "and deletes real implementation: the gateway's entire service lives in one, and an empty "
        "marker contributes 0 statements anyway."
    )


def test_coverage_is_not_computed_on_every_run() -> None:
    """A measurement nothing reads is pure cost, on the merge path and on every local run.

    There is no `fail_under`, no xml report, no artifact upload and no threshold in `ci.yml` or
    `.dagger/`, so `--cov` in `addopts` bought nobody anything. `make coverage` computes it on request.
    Kept as a test rather than a comment because the easiest way to "fix" a slow suite is to re-add a
    flag that makes it slower.
    """
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]

    assert "--cov" not in addopts, (
        f"`--cov` is back in pytest addopts ({addopts!r}), so every invocation in the estate pays for a "
        "coverage run. Nothing gates the result. Use `make coverage` when you want the number."
    )
