"""Every live e2e make target must REQUIRE its target, not skip into a green.

Measured 2026-08-23 against the deployed estate: `uv run pytest tests/e2e-py -m e2e` reported
**"3 passed, 84 skipped, 1 xfailed"** and exited **0**. `make e2e-medallion` — the target whose help
text calls it the "Medallion bronze→silver→gold cascade proof" — collected one test, skipped it, and
exited 0. Thirteen of the fourteen suites behaved the same way, so the whole live gate was a set of
proofs that could not fail.

This is the same defect `e2e-gateway` already carries the fix for, in a comment that states the rule:
"A live drive with no live target is a failed invocation, not a pass." That target guards
`LANCE_E2E_GATEWAY_URL` and exits 1 when it is unset. Nothing extended the rule to its thirteen
siblings, and nothing noticed, because the failure mode of the bug IS a green run.

The gate has two halves, and the second is what keeps it alive:

1. every target in ``REQUIRED_ENV`` guards each of its variables in its own recipe, and
2. every ``e2e-*`` target the Makefile actually defines appears in ``REQUIRED_ENV`` — so adding a
   fourteenth suite without deciding what makes it a real drive fails HERE.

``REQUIRED_ENV`` lists only the variables a suite's own ``pytest.skip`` is keyed on — the gating ones.
``LANCE_E2E_DAPR_TOKEN`` is deliberately absent: it is empty on a token-less dev stack, where the
producer's guard is a documented no-op, so requiring it would refuse a legitimate invocation.
"""

from __future__ import annotations

import re
from pathlib import Path


MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"

#: target stem -> the env vars its suite's own skip guards are keyed on.
REQUIRED_ENV: dict[str, tuple[str, ...]] = {
    "auth": ("LANCE_E2E_AUTH_SERVER",),
    "cas": ("LANCE_E2E_S3_ENDPOINT",),
    "compaction": ("LANCE_E2E_MAINTENANCE_URL", "LANCE_E2E_GREPTIME_URL"),
    "duckdb": ("LANCE_E2E_S3_ENDPOINT",),
    "dummy-lane": ("LANCE_E2E_CATALOG_URL",),
    "gateway": ("LANCE_E2E_GATEWAY_URL",),
    "governed-union": ("LANCE_E2E_LANCERAY_URL", "LANCE_E2E_LINEAGE_URL", "LANCE_E2E_FGA"),
    "medallion": ("LANCE_E2E_LANCERAY_URL", "LANCE_E2E_LINEAGE_URL"),
    "media": ("LANCE_E2E_LANCERAY_URL", "LANCE_E2E_LINEAGE_URL"),
    "media-catalog": ("MEDIA_CATALOG_URL",),
    "observability": ("LANCE_E2E_CATALOG_URL", "LANCE_E2E_LINEAGE_URL", "LANCE_E2E_GREPTIME_URL"),
    "user-state": ("LANCE_E2E_CATALOG_URL",),
    "ray-batch": ("LANCE_E2E_RAY_HEAD_DEPLOY",),
    "ray-train": ("LANCE_E2E_LANCERAY_URL", "LANCE_E2E_CATALOG_URL", "LANCE_E2E_LINEAGE_URL", "LANCE_E2E_FGA"),
    "isolation": ("LANCE_E2E_CATALOG_URL",),
}

#: Targets that BRING UP the stack they then drive, so there is no external target to require: both
#: shell out to a script that provisions a kind cluster and exports the suite's env itself. Exempt by
#: name rather than by pattern, so a future target cannot join them by accident.
STACK_PROVISIONING = frozenset({"ci", "ray-ci"})


def _recipes() -> dict[str, str]:
    """Every ``e2e-<stem>`` target's recipe body, keyed by stem.

    A recipe is the tab-indented block following ``<target>:``; it ends at the first line that is
    neither tab-indented nor blank.
    """
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    found: dict[str, str] = {}
    for index, line in enumerate(lines):
        match = re.match(r"^e2e-([a-z0-9-]+):", line)
        if not match:
            continue
        body: list[str] = []
        for following in lines[index + 1 :]:
            if following.startswith("\t"):
                body.append(following)
            elif following.strip():
                break
        found[match.group(1)] = "\n".join(body)
    return found


def test_every_e2e_target_is_accounted_for() -> None:
    """A new suite must declare what makes it a real drive, or this gate is already stale."""
    defined = set(_recipes())
    assert defined, f"no e2e-* targets parsed out of {MAKEFILE} — the recipe parser is broken"
    undeclared = defined - set(REQUIRED_ENV) - STACK_PROVISIONING
    assert not undeclared, (
        f"e2e targets with no entry in REQUIRED_ENV: {sorted(undeclared)}. Add the env its tests skip "
        "on, so the target fails rather than reporting a proof it did not run."
    )


def test_every_e2e_target_requires_its_env() -> None:
    """The rule `e2e-gateway` states, applied at every site: no live target, no pass."""
    recipes = _recipes()
    missing: list[str] = []
    for stem, variables in REQUIRED_ENV.items():
        recipe = recipes.get(stem)
        if recipe is None:
            continue  # covered by the accounted-for test; a stale table entry is not this test's failure
        for variable in variables:
            if f'test -n "$({variable})"' not in recipe:
                missing.append(f"e2e-{stem} does not require {variable}")
    assert not missing, "these targets skip into a green instead of failing when their live target is absent:\n  " + "\n  ".join(missing)
