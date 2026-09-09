"""Every OpenLineage `producer` URI the estate stamps must name THIS repository and a file in it.

`producer` is spec-required and is the only pointer an operator reading a run event has back to the
code that emitted it. A wrong one does not fail anything — it 404s in a browser, months later, for
someone trying to understand a run.

MEASURED 2026-09-09, which is why this is estate-wide rather than per-service. Five producer constants
exist and they used THREE spellings of the repository:

    services/lineage/.../seed.py                    Borg93/lance-ns      (the retired repo)
    services/lineage/.../repository.py              Borg93/lance-ns      (the retired repo)
    services/medallion/.../schemas/events.py        Borg93/lance-ns      (the retired repo)
    services/catalog/.../core/lineage_emit.py       Borg93/rask          (wrong org)
    services/maintenance/.../core/lineage_emit.py   AI-Riksarkivet/rask  (correct)

and the live graph carries the retired name on real runs — 738 medallion runs under
`Borg93/lance-ns/tree/main/medallion` alone.

WHY THE EXISTING GATE DID NOT CATCH IT, which is the reusable part: `services/catalog/tests/
test_lineage_producer_uri_is_real.py` was written for exactly this defect, checks the PATH half against
the real tree — and hardcodes the ORG half as `Borg93/rask`, itself wrong. A gate written to catch a
stale repository name carried a stale repository name, and it only ever looked at one of the five.

THE REPO IS READ FROM `git remote`, never written down here. A literal is what went stale twice.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
#: EVERY module-level constant holding a github URL, not only the ones named PRODUCER.
#:
#: THIS GATE SHIPPED NAME-MATCHING ON `PRODUCER` AND MISSED ONE WITHIN THE HOUR. `events.py` also
#: defines `_REPO_URL`, emitted as the SourceCodeLocationJobFacet's `url`, and it still named the
#: retired repository after every PRODUCER had been repointed — the same shape this file was written to
#: catch, in the file written to catch it. Matching on the VALUE (a github URL) rather than the NAME is
#: what makes the next differently-named constant covered without anyone remembering to add it.
_GITHUB = "github.com/"


def _canonical_repo() -> str:
    """`https://github.com/<org>/<repo>` from the checkout's own remote.

    Derived rather than declared: the two literals this gate replaces were each correct when written
    and wrong within a release, and a third literal here would have the same shelf life.
    """
    url = subprocess.run(  # noqa: S603
        ["git", "-C", str(REPO), "remote", "get-url", "origin"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return url.removesuffix(".git")


def _producers() -> list[tuple[Path, str, str]]:
    """Every `(file, constant, uri)` the estate defines, found by PARSING rather than grepping.

    A grep for the URI text finds the citations in prose too; the assertion is about what a running
    service STAMPS, which is a module-level string assignment and nothing else.
    """
    found: list[tuple[Path, str, str]] = []
    for root in ("packages", "services"):
        for path in (REPO / root).rglob("*.py"):
            if "/tests/" in str(path) or path.name.startswith("test_"):
                continue
            try:
                tree = ast.parse(path.read_text(errors="ignore"))
            except SyntaxError:
                continue
            for node in tree.body:
                targets = [node.target] if isinstance(node, ast.AnnAssign) else list(node.targets) if isinstance(node, ast.Assign) else []
                value = getattr(node, "value", None)
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    continue
                for t in targets:
                    name = getattr(t, "id", "")
                    if name and _GITHUB in value.value:
                        found.append((path, name, value.value))
    return found


def test_the_estate_defines_producer_uris_at_all() -> None:
    """The precondition: with none found, every assertion below passes vacuously and this file is
    decoration. It also pins the DISCOVERY — a rename of the constant would otherwise empty the gate
    silently, which is how the per-service version stayed narrow."""
    assert _producers(), "no PRODUCER constant found in packages/ or services/ — the parse has drifted from the code"


@pytest.mark.parametrize("case", _producers(), ids=lambda c: str(c[0].relative_to(REPO)))
def test_each_producer_uri_names_this_repository_and_an_existing_path(case: tuple[Path, str, str]) -> None:
    """Both halves, because each has failed on its own: the catalog stamped the right repo at a path
    that never existed, and three services stamped a real path under a repository that no longer holds
    the code."""
    path, name, uri = case
    repo = _canonical_repo()
    assert uri.startswith(repo), f"{path.relative_to(REPO)}::{name} points at {uri!r}, not at this repository ({repo})"

    # A BARE REPO URL IS LEGITIMATE and carries no path to check — `_REPO_URL` is the
    # SourceCodeLocationJobFacet's repository, which names the repo and nothing inside it. Only a
    # `/tree/main/<path>` form makes a claim about a file, and only that form is checked against the tree.
    prefix = f"{repo}/tree/main/"
    if not uri.startswith(prefix):
        return
    relative = uri.removeprefix(prefix)
    assert relative, f"{path.relative_to(REPO)}::{name} carries no path"
    assert (REPO / relative).exists(), f"{path.relative_to(REPO)}::{name} points at {relative!r}, which does not exist here — the link 404s"
