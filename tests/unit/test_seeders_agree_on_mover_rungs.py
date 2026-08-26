"""Two seeders may not grant one identity different rungs on the same medallion tier.

MEASURED on the live estate 2026-08-26. A silver->gold mover was refused its own tier::

    POST /v1/table/bind86-gold$catalog/describe  -> 403
    POST /v1/table/bind86-gold$catalog/create    -> 403

The cause was not the model, which is correct, and not the service identity, which resolves. It was
that the estate had TWO seeders with opposite answers for `service-silver-to-gold`:

    scripts/seed_medallion_fga.sh   ->  validator   (yields only can_promote)
    scripts/seed_estate.py          ->  owner

and `seed_estate.py` had ALREADY measured why `validator` is wrong, in a comment above the grant:
"The first attempt granted `validator` ... and it failed identically, three more 403s. `publish` is
guarded by `can_update_tag`, and the model says `define can_update_tag: owner`; `validator` buys
`can_promote`, which is the OTHER door on that route."

So the finding had been made, written down, and applied to exactly one of the two files. A tenant
seeded by the other one got a cascade that runs bronze->silver, lands rows, emits lineage, and dies
at the last hop — which reads as a permissions mystery rather than a seeding gap, because the catalog
runs its authorization gate BEFORE existence resolution and a 403 is what both look like.

WHY A TEST AND NOT A COMMENT. The two files are in different languages, live in different directories,
and are run by different people at different times; nothing reads both. A divergence between them is
invisible in review by construction, and its symptom appears hours later in a mover log. The rung is
also the kind of thing a well-meaning least-privilege edit will "tighten" back to `validator` — the
comment explains why that fails, and this makes the explanation enforceable.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
_SHELL = REPO / "scripts/seed_medallion_fga.sh"
_PYTHON = REPO / "scripts/seed_estate.py"

#: The cascade identities whose rung decides whether a tier can be written at all.
_MOVERS = ("service-silver-to-gold", "service-bronze-to-silver", "service-medallion-producer")

#: Rungs that actually carry `can_create_table` (= writer) and `can_update_tag` (= owner). `validator`
#: is deliberately absent: it yields `can_promote` and nothing else, which is the whole finding.
_SUFFICIENT = {"owner"}


def _shell_grants() -> dict[str, set[str]]:
    """{identity: {rungs}} from the `w <user> <relation> <object>` lines that name a medallion tier."""
    grants: dict[str, set[str]] = {}
    for line in _SHELL.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped.startswith("w "):
            continue
        parts = stripped.split()
        if len(parts) < 4:
            continue
        _, user, relation, obj = parts[0], parts[1], parts[2], " ".join(parts[3:])
        if "namespace:" not in obj:
            continue
        if not re.search(r"(bronze|silver|gold)", obj):
            continue
        grants.setdefault(user.removeprefix("user:"), set()).add(relation)
    return grants


def _python_grants() -> dict[str, set[str]]:
    """{identity: {rungs}} from `Grant("user:x", "rung", "namespace:...-tier")` calls."""
    grants: dict[str, set[str]] = {}
    text = _PYTHON.read_text(encoding="utf-8")
    for user, relation, obj in re.findall(r'Grant\(\s*"user:([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"(namespace:[^"]+)"\s*\)', text):
        if not re.search(r"(bronze|silver|gold)", obj):
            continue
        grants.setdefault(user, set()).add(relation)
    return grants


def test_both_seeders_are_actually_parsed() -> None:
    """Non-vacuity: a regex that stopped matching would report perfect agreement about nothing."""
    shell, python = _shell_grants(), _python_grants()
    assert shell, f"parsed no tier grants out of {_SHELL.name} — its grant syntax moved"
    assert python, f"parsed no tier grants out of {_PYTHON.name} — its Grant() shape moved"
    assert "service-silver-to-gold" in shell, f"{_SHELL.name} no longer grants the gold mover anything"
    assert "service-silver-to-gold" in python, f"{_PYTHON.name} no longer grants the gold mover anything"


def test_a_cascade_mover_is_granted_a_rung_that_can_actually_write_its_tier() -> None:
    """`validator` yields `can_promote` only — it can neither describe nor create the table it promotes."""
    offenders: list[str] = []
    for source, grants in ((_SHELL.name, _shell_grants()), (_PYTHON.name, _python_grants())):
        for identity in _MOVERS:
            rungs = grants.get(identity)
            if rungs and not (rungs & _SUFFICIENT):
                offenders.append(f"{source}: {identity} gets {sorted(rungs)}, none of which carries can_create_table/can_update_tag")

    assert not offenders, (
        "a cascade identity is seeded with a rung that cannot write the tier it owns, so its stage 403s on describe and create:\n  " + "\n  ".join(offenders)
    )


def test_the_two_seeders_do_not_contradict_each_other() -> None:
    """The same identity, the same tier, two files, two answers — the shape of the original defect."""
    shell, python = _shell_grants(), _python_grants()
    conflicts = [
        f"{identity}: {_SHELL.name} grants {sorted(shell[identity])}, {_PYTHON.name} grants {sorted(python[identity])}"
        for identity in sorted(set(shell) & set(python))
        if not (shell[identity] & python[identity])
    ]

    assert not conflicts, (
        "these identities are granted disjoint rungs by the two seeders, so which one a tenant gets "
        "depends on which script happened to run:\n  " + "\n  ".join(conflicts)
    )
