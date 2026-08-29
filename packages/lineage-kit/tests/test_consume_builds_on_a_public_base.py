"""`consume.py` must not reach across a module boundary for a private name (PS-26).

`from lineage_kit.schemas import Dataset, JobRef, RunEvent, _Model` — and then three public,
exported models (`DatasetRef`, `LineageEdge`, `LineageDoc`) subclassed that underscore name. The
leading underscore is a claim: "module-private, may change without notice". It was false, since a
sibling module's public API inherited from it, and every adopter writing their own consume-side model
had the choice of copying the config or importing a private name.

The base is part of the package's contract, so it carries a public name and is exported.
"""

from __future__ import annotations

import lineage_kit
from lineage_kit.consume import DatasetRef, LineageDoc, LineageEdge
from lineage_kit.schemas import RunEvent, WireModel


def test_the_alias_tolerant_base_is_public_and_exported() -> None:
    assert lineage_kit.WireModel is WireModel
    assert "WireModel" in lineage_kit.__all__


def test_no_module_reaches_into_schemas_for_a_private_base() -> None:
    from pathlib import Path

    src = Path(lineage_kit.__file__).resolve().parent
    offenders = {p.name: line for p in src.rglob("*.py") for line in p.read_text(encoding="utf-8").splitlines() if "import" in line and "_Model" in line}
    assert not offenders, f"a private base is imported across a module boundary: {offenders}"


def test_the_consume_models_still_share_the_wire_config() -> None:
    """Alias-tolerant in, unknown wire keys ignored — the behaviour the shared base exists for."""
    for model in (DatasetRef, LineageEdge, LineageDoc, RunEvent):
        assert issubclass(model, WireModel)
    ref = DatasetRef.model_validate({"namespace": "rask", "name": "bronze$pages", "nonsense": 1})
    assert ref.name == "bronze$pages"
