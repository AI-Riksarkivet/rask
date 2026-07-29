"""Every command CI invokes must resolve to something that exists.

The sibling gate (``test_e2e_collection_gate``) proves that pytest PATHS named by CI resolve. This
is the same defect one level up: CI also invokes **Dagger functions** and **make targets** by name,
and those had rotted the same way, unnoticed, because a name that does not exist fails at RUN time
in a job nobody reads to the bottom of.

Three were dead simultaneously in the ``test`` job:

* ``dagger call test-pg`` — the module has no ``TestPg`` function, and the plane it claimed to test
  (alembic + an app database) was deleted at P7a. It could only ever fail.
* ``make prod-render-check`` and ``make alert-rules-check`` — invoked by ``.dagger/charts.go`` and
  never defined, so ``dagger call charts`` died at the first of them. Both assets specified their own
  commands (``scripts/prod_render_check.sh``'s header says "Run: ``make prod-render-check``";
  ``chart/alerting/rules_test.yml``'s says this target runs check-then-test) — only the wrappers were
  missing, so the prod-overlay HA/security invariants and the alert-rule proofs had never run once.

The cost is not one red job. ``lineage-e2e``, ``e2e-stack`` and ``ray-e2e`` all declare
``needs: test``, so a ``test`` that cannot pass SKIPS every live proof in the repo — they report
neither success nor failure, and the branch looks merely "red somewhere" rather than "no live
coverage ran at all". That is the same silent-loss shape as the e2e path rot, one layer up.

Both directions are checked deliberately: a name CI calls must exist, so the reverse (an unused
Makefile target or Dagger function) is NOT an error — plenty exist for humans.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

#: `dagger call <fn>` — the kebab name Dagger exposes for a Go method on `*Rask`.
_DAGGER_CALL = re.compile(r"dagger call ([a-z][a-z0-9-]*)")
#: `func (m *Rask) Name(` — the exported methods Dagger turns into callable functions.
_DAGGER_FUNC = re.compile(r"func \(m \*Rask\) ([A-Z][A-Za-z0-9]*)\s*\(")
#: `make <target>` in a workflow step, and `[]string{"make", "<target>"}` in the Dagger module.
#: The Go pattern is anchored to `[]string{` on purpose: an unanchored `"make", "x"` also matches the
#: apt-get package list `{"apt-get", "install", …, "make", "curl", …}`, where `make` and `curl` are
#: packages being installed, not a target being invoked.
_MAKE_SHELL = re.compile(r"(?:^|[\s;&|])make\s+([a-z][a-z0-9-]*)")
_MAKE_EXEC = re.compile(r"\[\]string\{\"make\",\s*\"([a-z][a-z0-9-]*)\"")
#: A Makefile rule: `target:` at the start of a line (`.PHONY` and pattern rules excluded).
_MAKE_TARGET = re.compile(r"^([a-z][a-z0-9-]*)\s*:(?!=)", re.MULTILINE)

_WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
_DAGGER_GO = sorted((REPO_ROOT / ".dagger").glob("*.go"))


def _text(paths: list[Path]) -> str:
    """Concatenated source with COMMENT lines stripped.

    A comment naming a command is documentation, not an invocation — ci.yml explains why the dead
    `dagger call test-pg` step was removed, and a gate that reads that as a live call would demand the
    function be resurrected to describe its own deletion.
    """
    lines = [line for p in paths for line in p.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith(("#", "//"))]
    return "\n".join(lines)


def _pascal(kebab: str) -> str:
    """`test-lineage` -> `TestLineage`, the Go method name Dagger derives its CLI name from."""
    return "".join(part.capitalize() for part in kebab.split("-"))


def test_every_dagger_call_resolves_to_a_function() -> None:
    assert _DAGGER_GO, "no .dagger/*.go found — the gate would pass vacuously"
    defined = set(_DAGGER_FUNC.findall(_text(_DAGGER_GO)))
    assert defined, "no `func (m *Rask) X(` matched — the pattern no longer fits the module"

    called = set(_DAGGER_CALL.findall(_text(_WORKFLOWS + [REPO_ROOT / "Makefile"])))
    assert called, "no `dagger call` found in CI — the gate would pass vacuously"

    missing = sorted(name for name in called if _pascal(name) not in defined)
    assert not missing, (
        f"CI invokes Dagger functions that do not exist: {missing} (defined: {sorted(defined)}) — "
        "the step fails at run time, and every job declaring `needs: <that job>` is SKIPPED rather "
        "than run, so the branch loses live coverage without reporting a failure for it"
    )


def test_every_make_target_ci_invokes_exists() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(_MAKE_TARGET.findall(makefile))
    assert targets, "no targets parsed out of the Makefile — the pattern no longer fits"

    invoked: set[str] = set(_MAKE_EXEC.findall(_text(_DAGGER_GO)))
    for wf in _WORKFLOWS:
        for line in wf.read_text(encoding="utf-8").splitlines():
            # `run:` steps only — a bare "make" inside prose would otherwise register as an invocation.
            if "run:" in line or line.strip().startswith(("make ", "- make ")):
                invoked |= set(_MAKE_SHELL.findall(line))

    missing = sorted(name for name in invoked if name not in targets)
    assert not missing, f'CI invokes make targets that do not exist: {missing} — `make` exits 2 with "No rule to make target", failing the step that calls it'


@pytest.mark.parametrize("target", ["prod-render-check", "alert-rules-check"])
def test_the_chart_gate_targets_exist(target: str) -> None:
    """Named explicitly, not just covered by the sweep above.

    These two are what `dagger call charts` shells out to, and they went missing for long enough that
    the prod-overlay and alert-rule invariants never ran at all. A regression here is silent in a way
    the generic sweep would still catch — but naming them says WHICH gate stopped running.
    """
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert target in _MAKE_TARGET.findall(makefile), (
        f"`make {target}` is gone — .dagger/charts.go calls it, so `dagger call charts` fails and the "
        "prod-overlay HA/security invariants (or the promtool alert-rule proofs) stop running entirely"
    )
