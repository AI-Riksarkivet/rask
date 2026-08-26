"""The seeder must name a tier exactly as the cascade will ask for it.

MEASURED on tenant `bind86`, 2026-08-26. Its warehouse held::

    bind86-bronze     (qualified)
    silver            (unqualified leftover)
    <no gold at all>

and the cascade ran bronze->silver, landed rows, emitted lineage, held the promotion for review — and
then died asking for `bind86-gold$catalog` with a 403 that was really a 404, because the catalog runs
its authorization gate BEFORE existence resolution and the two are indistinguishable from outside.

THE MECHANISM. With `medallion.projectsEnabled`, `medallion.workflow._qualified` prefixes
`<project>-` at runtime, so a chart that declares `gold` produces `bind86-gold`. But
`seed_medallion_namespaces.py` read the chart's BARE names and had no `--project` at all, so it could
only ever create the unqualified set: namespaces the cascade will never ask for, and none of the ones
it will. Every tenant's tiers were therefore unprovisioned by construction, and the failure surfaced
one hop from the end, in a mover log, as a permissions error.

That script's own docstring already names this failure class, one level up — "authorization and
existence were seeded by different files and only one of them ran". It recurred one level down
because the two files agreed about the NAME and disagreed about the PROJECT.

WHY PIN THEM AGAINST EACH OTHER. Two implementations of one rule, in two languages, in two
directories, run by different people at different times. Nothing reads both, a divergence is
invisible in review, and the symptom appears hours later as a 403 nobody can attribute. Comparing the
functions directly is the only check that stays true when either one is edited.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]


def _seed_qualified():
    """Load the seeder's `qualified` without importing the whole script as a module path."""
    spec = importlib.util.spec_from_file_location("_seed_ns", REPO / "scripts/seed_medallion_namespaces.py")
    assert spec and spec.loader, "seed_medallion_namespaces.py is not importable"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.qualified


def _runtime_qualified():
    from medallion.workflow import _qualified

    return _qualified


#: The cases that matter, including the two that are easy to get wrong: an ALREADY-qualified name must
#: not be double-prefixed (the cascade re-qualifies its own output on every hop), and an empty project
#: must leave the name untouched (a single-tenant estate has no prefix).
_CASES = [
    ("bind86", "gold", "bind86-gold"),
    ("bind86", "silver", "bind86-silver"),
    ("bind86", "bronze-media", "bind86-bronze-media"),
    ("bind86", "bind86-gold", "bind86-gold"),
    ("acme", "gold", "acme-gold"),
    ("", "gold", "gold"),
    ("", "acme-gold", "acme-gold"),
]


@pytest.mark.parametrize(("project", "name", "expected"), _CASES)
def test_the_seeder_and_the_runtime_agree(project: str, name: str, expected: str) -> None:
    seed, runtime = _seed_qualified(), _runtime_qualified()

    assert seed(project, name) == expected, f"the SEEDER would provision {seed(project, name)!r}"
    assert runtime(project, name) == expected, f"the RUNTIME would ask for {runtime(project, name)!r}"
    assert seed(project, name) == runtime(project, name), (
        "the seeder provisions one name and the cascade asks for another — the tier will not exist "
        "when the mover reaches it, and the refusal will read as a permissions error"
    )


def test_the_seeder_can_target_a_project_at_all() -> None:
    """The original defect was not a wrong rule, it was a MISSING flag: there was no way to say which tenant."""
    text = (REPO / "scripts/seed_medallion_namespaces.py").read_text(encoding="utf-8")
    assert '"--project"' in text, "seed_medallion_namespaces.py cannot target a tenant, so no tenant's tiers can be provisioned"


def test_the_names_the_seeder_would_actually_create_are_qualified() -> None:
    """The rule must be APPLIED, not merely present.

    An earlier version of this file checked `qualified()` in isolation and a string in the call site.
    Both survived deleting the qualification from `declared_namespaces` — the helper still existed and
    still worked, and the seeder went back to provisioning the wrong names in silence. The only honest
    check is the list the seeder would POST.
    """
    spec = importlib.util.spec_from_file_location("_seed_ns2", REPO / "scripts/seed_medallion_namespaces.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    values = REPO / "chart/values.yaml"
    unqualified = module.declared_namespaces(values)
    assert unqualified, "the chart declares no medallion namespaces — nothing would be seeded at all"
    assert all(not n.startswith("bind86-") for n in unqualified), f"a project-less seed must use bare tier names: {unqualified}"

    for_tenant = module.declared_namespaces(values, "bind86")
    assert for_tenant, "a project-scoped seed produced no namespaces"
    offenders = [n for n in for_tenant if not n.startswith("bind86-")]
    assert not offenders, (
        f"the seeder would create {offenders} while the cascade asks for bind86-prefixed names, so "
        "those tiers will not exist when a mover reaches them"
    )
    assert len(for_tenant) == len(unqualified), "qualification changed how MANY namespaces are seeded, which it must not"
