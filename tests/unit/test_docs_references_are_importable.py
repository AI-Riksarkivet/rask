"""Every `::: module` in the docs must name something importable, or the docs build dies at RELEASE.

`zensical build` with `mkdocstrings-python` IMPORTS each module a `::: dotted.path` directive names, so
the docs build is a real (if narrow) import-health check. `.github/workflows/docs.yml` triggers only on
`workflow_dispatch` and `release: published` — no `push`, no `pull_request` — so that check runs at the
worst possible moment: when someone is cutting a release.

And it was already failing. `docs/reference/storage.md` carried `::: storage.iiif` after the IIIF
read-through cache MOVED to `runners/htr` on 2026-08-17 — a move `CLAUDE.md` records, with the reason
(a source only one workload uses belongs in that runner). Nothing on the merge path could catch it,
because nothing on the merge path ran the docs.

WHY THIS IS A TEST RATHER THAN WIRING THE DOCS BUILD INTO CI. The build also installs with `pip` in an
estate whose toolchain rule is uv, and putting it on every merge is a CI decision with a real time
cost. The IMPORT half — the part that actually caught a defect — is a few milliseconds here, on the
merge path, today. The rest of the docs build (rendering, theming, nav) can stay where it is.

The section was DELETED rather than repointed at `runners/htr`. A runner is sealed: `CLAUDE.md` states
its internals are deliberately undocumented at the platform level, because documenting one modality's
module in the shared reference is exactly what makes that modality look privileged.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS = REPO_ROOT / "docs"

#: `::: some.dotted.path`, mkdocstrings' identifier syntax, at the start of a line.
_DIRECTIVE = re.compile(r"^:::\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$", re.MULTILINE)


def _references() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in sorted(_DOCS.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for match in _DIRECTIVE.finditer(text):
            line = text[: match.start()].count("\n") + 1
            found.append((path.relative_to(REPO_ROOT).as_posix(), line, match.group(1)))
    return found


@pytest.mark.parametrize(("rel", "line", "target"), _references(), ids=lambda v: str(v))
def test_every_documented_module_can_be_imported(rel: str, line: int, target: str) -> None:
    try:
        importlib.import_module(target)
    except ImportError as exc:
        pytest.fail(
            f"{rel}:{line} documents `{target}`, which cannot be imported: {exc}. `zensical build` "
            "imports every module a ::: directive names, so this fails the docs build — and the docs "
            "workflow runs only on release, so nobody finds out until they are cutting one."
        )


def test_the_scan_finds_the_reference_pages() -> None:
    """Non-vacuity: an empty scan parametrizes zero cases and the file passes having checked nothing.

    Parametrized gates fail this way silently — pytest reports the file as passed with no tests, which
    reads identically to a clean estate.
    """
    references = _references()
    pages = sorted(p.relative_to(REPO_ROOT).as_posix() for p in (_DOCS / "reference").glob("*.md"))
    covered = {rel for rel, _, _ in references}

    # DERIVED, not a magic number. The first version of this floor was `>= 10`, set against the
    # seventeen directives that existed before the sealed-runner pages were deleted — and it went RED
    # on a change that made the estate MORE correct. A remembered count measures when the docs were
    # written; this measures that every reference page still carries at least one directive, which is
    # the property that actually makes the gate non-vacuous.
    assert pages, "docs/reference/ has no pages — the docs layout moved and this gate is vacuous"
    uncovered = [p for p in pages if p not in covered]
    assert not uncovered, (
        f"these reference pages carry no ::: directive, so nothing about them is import-checked: "
        f"{uncovered}. A reference page that documents nothing is either stale or misfiled."
    )


def test_every_nav_entry_points_at_a_page_that_exists() -> None:
    """A nav entry naming a deleted page is a broken link at best and a 404 in the built site.

    Ten were dead when this gate was written — eight pointing at working documents that had been
    deleted (`OPEN-WORK.md`, three `DESIGN-*`, two dated assessments), and two at the sealed-runner
    reference pages. The docs build runs only on `workflow_dispatch` and `release: published`, so
    nothing on the merge path had any reason to notice.

    This is the cheap half of the docs build, on the merge path, at no cost: existence, not rendering.
    """
    import re as _re

    toml = (REPO_ROOT / "zensical.toml").read_text(encoding="utf-8")
    targets = _re.findall(r'"([^"]+\.md)"', toml)

    assert targets, "no .md nav targets parsed out of zensical.toml — the config moved"
    missing = sorted({t for t in targets if not (_DOCS / t).is_file()})
    assert not missing, f"these nav entries name pages that do not exist, so the built site links into 404s: {missing}"
