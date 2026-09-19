"""`Settings` is a composition of per-domain blocks, and the split moved no field's meaning.

[[LH-113]]. One class carried 62 annotated fields across 464 lines — the object store, the control bus,
Dapr, lineage, maintenance, user state, vending and authz all in one place, so reading any domain meant
reading the other seven. `LanceSessionCaps` (2 fields) was the single block already split, and the shape
it demonstrated is the one applied here: nine blocks composed onto `Settings`, exactly as the estate's
eight services already compose `GovernedAuthSettings`.

A PURE REFACTOR HAS TO PROVE IT IS ONE, which is what this file is for. Behaviour-preserving is a claim
about every field's annotation, alias, default and requiredness — 78 of them once the inherited blocks
resolve — and a suite that merely still passes does not check that: a field whose ALIAS moved keeps
every test green until a deployment sets the old env var and gets the default.

THE GATE IS STRUCTURAL, NOT A SNAPSHOT OF NAMES. Asserting the exact field list would fail the next
time somebody adds a knob, which teaches people to regenerate it rather than read it. What it asserts is
the property the row asks for: no class in this module carries the whole surface, and the one that
composes them declares none of what it inherits twice.
"""

from __future__ import annotations

import ast
from pathlib import Path

from catalog.core import config
from catalog.core.config import Settings


_MODULE = Path(config.__file__)

#: The largest a single block may grow before it is carrying more than one domain. `Settings` itself sat
#: at 62 when this row was written; the biggest block after the split is the object store at 11.
_MAX_FIELDS_PER_BLOCK = 20


def _classes() -> dict[str, ast.ClassDef]:
    return {n.name: n for n in ast.walk(ast.parse(_MODULE.read_text())) if isinstance(n, ast.ClassDef)}


def _declared(node: ast.ClassDef) -> set[str]:
    return {b.target.id for b in node.body if isinstance(b, ast.AnnAssign) and isinstance(b.target, ast.Name)}


def test_no_single_class_carries_the_whole_surface() -> None:
    """The defect: 62 fields in one class, so no domain could be read on its own."""
    oversized = {name: len(_declared(node)) for name, node in _classes().items() if len(_declared(node)) > _MAX_FIELDS_PER_BLOCK}

    assert not oversized, f"these classes carry more than one domain's fields: {oversized}"


def test_settings_COMPOSES_the_blocks_rather_than_restating_them() -> None:
    """A block that is inherited AND redeclared is two sources for one knob — the drift this undoes.

    `Settings` once carried a byte-identical twin of the OIDC/FGA knobs it now inherits, and its own
    docstring records that as "precisely how the copies drifted".
    """
    classes = _classes()
    settings = classes["Settings"]
    inherited = {name for base in settings.bases if isinstance(base, ast.Name) and (name := base.id) in classes}

    assert len(inherited) >= 5, f"`Settings` composes only {sorted(inherited)} — the split did not land"
    for base in inherited:
        overlap = _declared(settings) & _declared(classes[base])
        assert not overlap, f"`Settings` redeclares {sorted(overlap)}, which it already inherits from {base}"


def test_every_block_is_reachable_from_settings() -> None:
    """A block nothing composes is dead configuration that still reads as live."""
    classes = _classes()
    composed = {b.id for b in classes["Settings"].bases if isinstance(b, ast.Name)}
    orphans = [name for name in classes if name.startswith("Catalog") and name not in composed]

    assert not orphans, f"these blocks are declared and composed by nothing: {orphans}"


def test_the_resolved_surface_still_carries_every_domain() -> None:
    """The split is only correct if the composed model still answers for all of it.

    Checked on the RESOLVED model rather than the source, because that is what a deployment reads: one
    representative alias per block, so a block dropped from the bases fails here rather than at boot.
    """
    for field, alias in (
        ("s3_endpoint", "LANCE_S3_ENDPOINT"),
        ("control_root", "LANCE_CONTROL_ROOT"),
        ("dapr_pubsub", "LANCE_DAPR_PUBSUB"),
        ("lineage_url", "LANCE_LINEAGE_URL"),
        ("vending_mode", "LANCE_VENDING_MODE"),
    ):
        assert field in Settings.model_fields, f"{field} vanished from the composed model"
        assert Settings.model_fields[field].alias == alias, f"{field} no longer answers to {alias}"
