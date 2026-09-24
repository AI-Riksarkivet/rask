"""The set of declared e2e suites that NO automation drives is recorded, so it cannot grow in silence.

`E2E_SUITES` in the Makefile declares seventeen `make e2e-<suite>` targets, each running
`pytest tests/e2e-py -m <marker>` against a live address the caller supplies. That design is right —
each target refuses to run without its target ("a live drive with no live target is a failed
invocation, not a pass"). What it does not say is whether anything ever invokes them.

MEASURED 2026-09-24: eleven of the seventeen are reached by no automation at all — not
`scripts/e2e_stack.sh`, not `scripts/ray_e2e_stack.sh`, not `ci.yml`. Ten of those eleven are at
least REACHABLE, by `make e2e-live` (`pytest tests/e2e-py -m e2e` against the deployed release), so
the gap for them is that nothing chooses to; `user-state` carried no `e2e` marker at all and was
reachable by its own target and nothing else, which this measurement is what found. That is the estate's most
expensive defect shape this week, three times over: `rustfs-lifecycle` and `governance-chain` had
each been broken for weeks before anyone ran them, and `spec-conformance` — in the list below — was
green against the deployed estate while the catalog returned 500 for the spec's own root-namespace
id on any store that validates object ids.

A LIST, NOT A BAN, for the reason the secret-env ratchet states: a test demanding zero would be red
on arrival and deleted within a week. This fails when the set CHANGES in either direction — a new
suite nothing drives, or one that got wired and should leave the list — so the number can be walked
down deliberately and cannot drift up quietly.

THE REASONS ARE NOT UNIFORM, and only two are recorded in the repo today. `chaos` is deliberate: its
own Makefile comment says it scales a Deployment to 0, so a shared lane running it would take
lineage down under every other suite. `spec-conformance` belongs in the kind stack's suite list,
which is blocked behind an unpullable object-store image ([[XC-075]]). For the other nine, the
absence of a recorded reason IS the finding.
"""

from __future__ import annotations

import pathlib
import re


REPO = pathlib.Path(__file__).resolve().parents[2]

#: Where a suite could be invoked from WITHOUT a human choosing to.
#:
#: `scripts/e2e_live.sh` is deliberately NOT here, and saying why matters because it makes the set
#: below look worse than it is: it runs `pytest tests/e2e-py -m e2e` against the deployed release and
#: therefore reaches ten of the eleven — but only when someone types `make e2e-live`, which is the
#: same "a human must choose to" that this gate is about. It is the difference between a suite that
#: CAN be run and one that IS run.
_AUTOMATION = ("scripts/e2e_stack.sh", "scripts/ray_e2e_stack.sh", ".github/workflows/ci.yml")

#: The broad marker that sweep selects. A declared suite carrying it is at least reachable by one
#: command; one that does not is reachable by its own `make` target and nothing else.
_BROAD_MARKER = "e2e"

#: Declared suites no automation reaches, measured 2026-09-24. **This set may only SHRINK.**
#: Wiring one means deleting its entry in the same commit; adding a suite nothing drives means
#: saying so here, next to the ten that already admit it.
UNDRIVEN = frozenset(
    {
        "auth",
        "chaos",
        "dummy-lane",
        "gateway",
        "governed-union",
        "media",
        "media-catalog",
        "medallion",
        "observability",
        "spec-conformance",
        "user-state",
    }
)


def _suites() -> dict[str, str | None]:
    """`{suite: pytest marker}` from the Makefile — both derived, neither restated here."""
    body = (REPO / "Makefile").read_text(encoding="utf-8")
    declared = re.search(r"^E2E_SUITES = (.+)$", body, re.MULTILINE)
    assert declared, "E2E_SUITES is gone from the Makefile — the gate lost its subject"
    out: dict[str, str | None] = {}
    for suite in declared.group(1).split():
        hit = re.search(rf"^e2e-{re.escape(suite)}:.*?\n(?:\t.*\n)*?\t.*pytest tests/e2e-py -m (\w+)", body, re.MULTILINE)
        out[suite] = hit.group(1) if hit else None
    return out


def _files_by_marker() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in sorted((REPO / "tests" / "e2e-py").glob("test_*.py")):
        for marker in set(re.findall(r"pytest\.mark\.(\w+)", path.read_text(encoding="utf-8"))):
            out.setdefault(marker, []).append(path.name)
    return out


def _undriven() -> set[str]:
    automation = "\n".join((REPO / name).read_text(encoding="utf-8") for name in _AUTOMATION)
    by_marker = _files_by_marker()
    return {suite for suite, marker in _suites().items() if not any(name in automation for name in by_marker.get(marker or "", []))}


def test_the_derivation_still_finds_suites_and_markers() -> None:
    """A control, because this gate is three regexes over two file formats.

    The first cut of this measurement matched no target at all and reported every suite as undriven —
    a scanner that silently finds nothing would otherwise make the set below look complete.
    """
    suites = _suites()
    assert len(suites) > 10, f"only {len(suites)} suites parsed out of E2E_SUITES"
    resolved = [suite for suite, marker in suites.items() if marker]
    assert len(resolved) == len(suites), f"these suites' pytest marker did not parse: {sorted(set(suites) - set(resolved))}"
    assert _files_by_marker(), "no e2e-py file declares a pytest marker — the marker scan broke"


def test_the_set_of_undriven_suites_is_the_recorded_one() -> None:
    found = _undriven()
    newly_undriven = sorted(found - UNDRIVEN)
    assert not newly_undriven, (
        f"these declared e2e suites are now reached by no automation: {newly_undriven}. A suite only a "
        f"human can invoke is one nobody invokes — three lanes rotted that way this week. Wire it into "
        f"{_AUTOMATION[0]} or {_AUTOMATION[2]}, or add it to UNDRIVEN with the reason."
    )
    now_driven = sorted(UNDRIVEN - found)
    assert not now_driven, (
        f"these are recorded as driven by nothing but something now drives them: {now_driven}. "
        f"Delete their entries in the same commit that wired them — a stale exemption is where the "
        f"next unrun lane hides."
    )


def test_every_declared_suite_is_at_least_reachable_by_the_live_sweep() -> None:
    """The floor under the list above: a suite no automation drives should at minimum be sweepable.

    `make e2e-live` selects the broad `e2e` marker against the deployed release. A declared suite
    whose files do not carry it is reachable by its own `make` target and by nothing else — not by
    automation and not by the one command an operator runs to exercise a live estate. Measured
    2026-09-24, `user-state` was exactly that, alone among the seventeen.
    """
    by_marker = _files_by_marker()
    broad = set(by_marker.get(_BROAD_MARKER, []))
    assert broad, f"no e2e-py file carries `pytest.mark.{_BROAD_MARKER}` — the sweep marker moved"
    stranded = sorted(suite for suite, marker in _suites().items() if (files := by_marker.get(marker or "", [])) and not any(name in broad for name in files))
    assert not stranded, (
        f"these declared suites carry no `pytest.mark.{_BROAD_MARKER}`, so even the live sweep skips "
        f"them: {stranded}. Add the broad marker alongside the suite's own, as "
        f"`test_medallion_e2e.py` does."
    )
