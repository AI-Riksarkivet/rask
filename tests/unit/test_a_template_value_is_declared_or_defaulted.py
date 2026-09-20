"""A value a template reads must be declared in `values.yaml` or carry its own default.

A reference to an undeclared value is not an error in Helm — it renders EMPTY. So a knob that
`values.yaml` never names is invisible twice over: a reader of the values file cannot discover it,
and a render that silently drops the line it controls looks identical to one that never wanted it.

MEASURED 2026-09-20, and the chart is clean: 390 distinct `.Values.*` references across the
templates, 798 declared keys, and the six undeclared ones are all deliberately optional — each
guarded on its own reference with `hasKey … | ternary` or `| default`, so the template supplies the
fallback itself. That is a legitimate pattern and this gate accepts it; what it refuses is the third
case, an undeclared reference with NO fallback, which is the one that renders empty and says nothing.

WHY THIS CLASS IS WORTH A RATCHET. The estate met the adjacent failure the same day: a deploy would
have turned the medallion quality gate off because `medallion.quality` defaults FALSE while the live
pods run it true — declared, defaulted, and still a surprise. An UNDECLARED value is that failure with
the documentation removed, and nothing would have reported it.

Deliberately not a check that every declared value is USED. An unused value is harmless and removing
one is a compatibility decision, not a correctness fix.
"""

from __future__ import annotations

import pathlib
import re

import yaml


REPO = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "chart/templates"
VALUES = REPO / "chart/values.yaml"

#: A dotted value path: `.Values.medallion.quality`.
_REF = re.compile(r"\.Values\.([A-Za-z0-9_.]+)")
#: A fallback supplied on the reference itself — the template taking responsibility for the default.
_GUARDED = re.compile(r"\bdefault\b|\bhasKey\b")


def _declared() -> set[str]:
    def walk(node: object, prefix: str = "") -> set[str]:
        if not isinstance(node, dict):
            return set()
        keys: set[str] = set()
        for key, value in node.items():
            path = f"{prefix}{key}"
            keys.add(path)
            keys |= walk(value, f"{path}.")
        return keys

    return walk(yaml.safe_load(VALUES.read_text(encoding="utf-8")))


def _unguarded_references() -> dict[str, str]:
    """value path -> the line that reads it, for references with neither a declaration nor a default."""
    declared = _declared()
    offenders: dict[str, str] = {}
    for path in sorted(TEMPLATES.rglob("*")):
        if path.suffix not in {".yaml", ".tpl"} or not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _GUARDED.search(line):
                continue
            for reference in _REF.findall(line):
                reference = reference.rstrip(".")
                if reference not in declared:
                    offenders.setdefault(reference, f"{path.relative_to(REPO)}:{number}")
    return offenders


def test_no_template_reads_a_value_that_is_neither_declared_nor_defaulted() -> None:
    offenders = _unguarded_references()

    assert not offenders, (
        "these templates read a value `values.yaml` does not declare and supply no fallback, so Helm "
        "renders it EMPTY and the line it controls disappears without a word — declare it in "
        "`values.yaml` or guard the reference with `| default`:\n  " + "\n  ".join(f"{ref}  ({where})" for ref, where in sorted(offenders.items()))
    )


def test_the_walk_reads_both_sides() -> None:
    """Without this, an empty template glob or an unparsed values file would pass vacuously."""
    references = {
        r.rstrip(".") for p in TEMPLATES.rglob("*") if p.suffix in {".yaml", ".tpl"} and p.is_file() for r in _REF.findall(p.read_text(encoding="utf-8"))
    }

    assert len(references) > 200, f"only {len(references)} value references found — the template walk is broken"
    assert len(_declared()) > 300, "values.yaml parsed to too few keys — the loader is not reading it"
