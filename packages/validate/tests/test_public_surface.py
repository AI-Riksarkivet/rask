"""`validate` exports only what something consumes (PS-13, PS-14).

`validate/rules.py` shipped five exported symbols — `Rule`, `max_file_size`, `image_dimensions`,
`allowed_extensions`, `validate` — with zero callers anywhere in the estate and zero tests. Not
"unused for now": a pluggable rules framework nothing plugs into, exported from the barrel, so every
reader of this package had to work out which half was real. The estate's rule is that dead code goes
in the change that kills it, cascading through the module and its barrel export, so it did.

That also settled PS-14 — `Rule` was a `@dataclass`, the only one in a Pydantic-first estate — by
deletion rather than by porting a value object nothing constructs.

The surviving surface is `images.py`: one coherent module, exercised by this package's own suite and
consumed by `services/ingest/src/ingest/validation.py`.
"""

from __future__ import annotations

from pathlib import Path

import validate


_SRC = Path(validate.__file__).resolve().parent


def test_the_barrel_exports_only_the_image_validators() -> None:
    assert set(validate.__all__) == {
        "ValidationError",
        "validate_by_extension",
        "validate_bytes_by_extension",
        "validate_jpg",
        "validate_jpg_bytes",
        "validate_png",
        "validate_png_bytes",
        "validate_tiff",
        "validate_tiff_bytes",
    }
    assert not hasattr(validate, "Rule")
    assert not hasattr(validate, "max_file_size")


def test_the_unconsumed_rules_module_is_gone() -> None:
    assert not (_SRC / "rules.py").exists(), "an exported module with no callers and no tests is dead code, not a feature"


def test_the_package_declares_no_dataclasses() -> None:
    """Pydantic-first estate: a value object is a `BaseModel`, never a `@dataclass`."""
    sources = {path.name: path.read_text(encoding="utf-8") for path in _SRC.rglob("*.py")}
    offenders = {name for name, text in sources.items() if "dataclass" in text}
    assert not offenders, f"@dataclass used for a value object: {offenders}"


def test_no_prose_still_advertises_the_deleted_rules_framework() -> None:
    """The package's own metadata and the repo map both sold "pluggable rules" as a feature."""
    repo = Path(__file__).resolve().parents[3]
    for doc in (repo / "CLAUDE.md", repo / "packages" / "validate" / "pyproject.toml"):
        assert "pluggable rules" not in doc.read_text(encoding="utf-8"), f"{doc.name} still advertises the deleted rules framework"
