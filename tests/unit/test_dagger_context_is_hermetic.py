"""A local `dagger call` must not ship the developer's `.env` into the container.

`.env` is untracked. CI checks out fresh and has none; a developer's machine has one — measured on this
host, 1470 bytes — and NONE of the four Dagger build contexts excluded it. So the same command took a
different, secret-bearing input depending on whose machine ran it, and the local run was the one that
diverged from CI rather than reproducing it. That is the opposite of what a hermetic build is for.

It compounds a real behaviour rather than being theoretical: `service_kit.build_settings()` calls
`load_dotenv()` and then `derive_hcp_creds()`, which writes derived credentials into `os.environ`
permanently. Every app any test builds inside that container would have read the developer's values.

Verified live, not reasoned. A probe counting `.env` entries in the container's `/src`:

    exclude removed (the pre-fix state):  2
    exclude present:                      0

The glob is `**/.env`, not `.env`, for the reason the estate already learned about Dagger's `+ignore`:
a bare root-relative pattern misses nested copies, and a service or runner directory may grow its own.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
_DAGGER = REPO_ROOT / ".dagger"

#: Every secret-bearing untracked file that must never enter a build context.
_MUST_EXCLUDE = ("**/.env", "**/.env.*")


def _exclude_lists() -> list[tuple[str, str]]:
    """(file, the Exclude literal) for every `WithDirectory` build context in the module."""
    found: list[tuple[str, str]] = []
    for path in sorted(_DAGGER.glob("*.go")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"Exclude:\s*\[\]string\{(.*?)\}", text, re.DOTALL):
            found.append((path.name, match.group(1)))
    return found


def test_every_dagger_build_context_excludes_the_developer_dotenv() -> None:
    lists = _exclude_lists()
    assert lists, "no Exclude lists parsed out of .dagger/*.go — the build contexts moved"

    offenders = [f"{name}: {' '.join(body.split())[:70]}" for name, body in lists if not all(pattern in body for pattern in _MUST_EXCLUDE)]
    assert not offenders, (
        "these Dagger build contexts would ship a developer's untracked .env into the container, so a "
        "local run takes an input CI cannot have:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_reaches_every_context_the_module_defines() -> None:
    """Non-vacuity: a regex that stopped matching would report a hermetic estate.

    The count is a floor rather than an equality because a new Dagger function with its own context is
    a normal thing to add — and the gate above is what makes adding one safe.
    """
    lists = _exclude_lists()
    files = {name for name, _ in lists}

    assert len(lists) >= 4, f"only {len(lists)} Exclude lists found; the module defines at least four"
    for expected in ("main.go", "test.go", "charts.go", "frontend.go"):
        assert expected in files, f"{expected} defines a build context and the scan no longer sees it"


@pytest.mark.parametrize("pattern", _MUST_EXCLUDE)
def test_the_exclusion_is_recursive_not_root_only(pattern: str) -> None:
    """`.env` rather than `**/.env` would miss a nested one, which is the estate's own prior lesson.

    The same mistake was already made with `+ignore` and bare `.venv`/`node_modules`: a root-relative
    pattern leaves `runners/*/.venv` in the context. A service or runner growing its own `.env` is the
    same shape.
    """
    assert pattern.startswith("**/"), f"{pattern} is root-relative and would miss a nested copy"
