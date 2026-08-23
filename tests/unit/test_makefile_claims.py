"""The Makefile's comments are read as documentation, and one of them was false for a long time.

`make test`'s comment asserted "the frontends have no unit suite — `make frontend-check`
(svelte-check) is their gate." The estate has **128 tracked vitest files** across the **18 packages
that declare a `test` script**, and CI runs them. Anyone reading the Makefile to find out what `make
test` covers was told the other plane had nothing to cover.

A comment is exactly the kind of assertion that rots: nothing executes it, nothing type-checks it, and
it survives every refactor that makes it false. This gate is narrow on purpose — it pins the specific
claims that were measured wrong, rather than pretending to validate prose in general.
"""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = REPO_ROOT / "Makefile"
_FRONTEND = REPO_ROOT / "frontend"


def _vitest_files() -> list[Path]:
    return [p for p in _FRONTEND.rglob("*.test.ts") if "node_modules" not in p.parts]


def _packages_with_a_test_script() -> list[str]:
    found: list[str] = []
    for manifest in sorted(_FRONTEND.glob("*/*/package.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:  # pragma: no cover — another gate's problem
            continue
        if "test" in data.get("scripts", {}):
            found.append(manifest.parent.name)
    return found


def test_the_makefile_does_not_deny_the_frontend_unit_suite() -> None:
    text = _MAKEFILE.read_text(encoding="utf-8")
    files, packages = _vitest_files(), _packages_with_a_test_script()

    assert files, "no vitest files found at all — the scan is broken, not the estate"
    assert packages, "no frontend package declares a test script — the scan is broken"

    assert "the frontends have no unit suite" not in text, (
        f"the Makefile denies the frontend unit suite, but {len(files)} vitest files exist across "
        f"{len(packages)} packages declaring a `test` script ({', '.join(sorted(packages)[:5])}, …). "
        "`bun --cwd=frontend run test` runs them and CI runs both planes."
    )


def test_the_frontend_suite_is_large_enough_that_denying_it_would_be_a_real_loss() -> None:
    """Non-vacuity with teeth: if the suite ever genuinely shrank to nothing, the claim above would
    become true and this file should be deleted rather than left asserting a negative about an empty
    set. Making that explicit is cheaper than discovering it as a confusing green."""
    assert len(_vitest_files()) >= 50, (
        f"only {len(_vitest_files())} vitest files remain; if the frontend suite really is gone, delete "
        "this gate and restore the Makefile's original claim rather than leaving both half-true"
    )
