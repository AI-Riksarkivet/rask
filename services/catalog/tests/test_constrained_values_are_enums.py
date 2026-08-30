"""catalog-api-16 — a constrained wire value is parsed ONCE, into an enum, not re-lowered per decision.

``create``'s ``mode`` arrives as a bare ``str | None`` and was re-derived four separate times against a
hand-written vocabulary: ``(mode or "").lower() not in ("existok", "exist_ok")`` for the compensation
rule, ``_mode = (mode or "").lower()`` plus two membership tests for the pre-existence guards,
``(mode or "").lower() in ("existok", "exist_ok")`` again for the schema read-back, and
``(mode or "create").lower()`` once more down in the data plane. Four copies of one vocabulary, on the
door where getting it wrong means an Overwrite that silently creates or an ExistOk that seizes
ownership. ``drop_namespace``'s ``behavior`` had the same shape.

The gate is the PATTERN, not the spelling: a lowercase-then-compare against one of these vocabularies
anywhere but the module that owns the enum. (``mode="overwrite"`` handed to pylance's own
``write_dataset`` is a different vocabulary and is not this.)
"""

from __future__ import annotations

import pathlib
import re

import pytest

from catalog.core.modes import CreateMode, DropBehavior


_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog"
_OWNER = _SRC / "core" / "modes.py"

#: Values that only ever appear as part of one of these constrained vocabularies.
_VOCABULARY = ("existok", "exist_ok", "overwrite", "cascade")

#: The hand-normalisation idiom, restricted to the two constrained PARAMETERS — so a `.lower()` on some
#: other field (a boolean-ish record flag, a pylance error message) is not swept up with them.
_HAND_NORMALISED = re.compile(r"\((?:\w+\.)?(?:mode|behavior)\s+or\s+\"[^\"]*\"\)\.lower\(\)")


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_walk_sees_the_catalog_source() -> None:
    assert len(_modules()) > 40, f"only {len(_modules())} modules — the walk is not seeing the catalog"


def test_no_module_re_lowers_a_constrained_value_to_compare_it() -> None:
    offences = [
        f"{path.relative_to(_SRC)}:{n}: {line.strip()}"
        for path in _modules()
        if path != _OWNER
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if _HAND_NORMALISED.search(line) or (".lower()" in line and any(f'"{v}"' in line for v in _VOCABULARY))
    ]
    assert not offences, "ad-hoc lowercase-and-compare against a constrained vocabulary — parse once into the enum:\n  " + "\n  ".join(offences)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, CreateMode.CREATE),
        ("", CreateMode.CREATE),
        ("create", CreateMode.CREATE),
        ("Create", CreateMode.CREATE),
        ("overwrite", CreateMode.OVERWRITE),
        ("Overwrite", CreateMode.OVERWRITE),
        ("OVERWRITE", CreateMode.OVERWRITE),
        ("existok", CreateMode.EXIST_OK),
        ("ExistOk", CreateMode.EXIST_OK),
        ("exist_ok", CreateMode.EXIST_OK),
    ],
)
def test_every_spelling_the_door_accepted_still_parses(raw: str | None, expected: CreateMode) -> None:
    """The four hand-written copies between them accepted all of these. A single parser that dropped
    one would turn an ExistOk into a create — the ownership-seizure case the guards exist for."""
    assert CreateMode.parse(raw) is expected


def test_an_unrecognised_mode_still_means_create_exactly_as_before() -> None:
    """PRESERVED, not endorsed: the four copies all fell through to create-semantics for an unknown
    value, so this parser does too. Turning a typo'd mode into a 400 is a behaviour change for
    existing callers and is not this finding's to make."""
    assert CreateMode.parse("Overwrit") is CreateMode.CREATE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DropBehavior.RESTRICT),
        ("cascade", DropBehavior.CASCADE),
        ("CASCADE", DropBehavior.CASCADE),
        ("restrict", DropBehavior.RESTRICT),
        ("nonsense", DropBehavior.RESTRICT),
    ],
)
def test_drop_behaviour_parses_the_same_way_it_compared(raw: str | None, expected: DropBehavior) -> None:
    assert DropBehavior.parse(raw) is expected
