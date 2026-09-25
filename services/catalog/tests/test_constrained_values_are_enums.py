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
from collections.abc import Callable
from enum import StrEnum

import pytest
from lance_namespace import ErrorCode, InvalidInputError

from catalog.core.modes import CreateMode, DropBehavior, DropMode, InsertMode, RegisterMode


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
        ("create", CreateMode.CREATE),
        ("Create", CreateMode.CREATE),
        ("overwrite", CreateMode.OVERWRITE),
        ("Overwrite", CreateMode.OVERWRITE),
        ("OVERWRITE", CreateMode.OVERWRITE),
        ("existok", CreateMode.EXIST_OK),
        ("ExistOk", CreateMode.EXIST_OK),
        ("exist_ok", CreateMode.EXIST_OK),
        ("EXIST_OK", CreateMode.EXIST_OK),
    ],
)
def test_every_spelling_the_spec_admits_parses(raw: str, expected: CreateMode) -> None:
    """The spec's words: case insensitive, PascalCase or snake_case. The vocabulary is closed, so a
    spelling the parser lost would refuse a correct ExistOk as InvalidInput rather than pass unnoticed."""
    assert CreateMode.parse(raw) is expected


#: Each closed vocabulary with its parser. A map rather than ``vocabulary.parse`` because ``StrEnum``
#: itself declares no ``parse``, and the typed callable is what lets one test body serve every set.
_PARSERS: dict[type[StrEnum], Callable[[str | None], StrEnum]] = {
    CreateMode: CreateMode.parse,
    RegisterMode: RegisterMode.parse,
    InsertMode: InsertMode.parse,
    DropMode: DropMode.parse,
    DropBehavior: DropBehavior.parse,
}


def _pascal(member: StrEnum) -> str:
    return "".join(word.capitalize() for word in member.value.split("_"))


@pytest.mark.parametrize("vocabulary", list(_PARSERS), ids=lambda v: v.__name__)
def test_each_member_parses_from_both_spellings_in_any_case(vocabulary: type[StrEnum]) -> None:
    """The rule is stated once in `modes.py`, so it must hold for every set and not only the create one."""
    parse = _PARSERS[vocabulary]
    for member in vocabulary:
        for spelling in (member.value, member.value.upper(), _pascal(member), _pascal(member).lower()):
            assert parse(spelling) is member, f"{vocabulary.__name__}.parse({spelling!r})"


@pytest.mark.parametrize(
    ("vocabulary", "default"),
    [
        (CreateMode, CreateMode.CREATE),
        (RegisterMode, RegisterMode.CREATE),
        (InsertMode, InsertMode.APPEND),
        (DropMode, DropMode.FAIL),
        (DropBehavior, DropBehavior.RESTRICT),
    ],
    ids=lambda v: getattr(v, "__name__", str(v)),
)
@pytest.mark.parametrize("absent", [None, ""], ids=["none", "blank"])
def test_an_absent_value_is_the_spec_default(vocabulary: type[StrEnum], default: StrEnum, absent: str | None) -> None:
    """Absent is the spec's own "(default)"; blank is how an empty query parameter arrives."""
    assert _PARSERS[vocabulary](absent) is default


@pytest.mark.parametrize(
    ("vocabulary", "raw"),
    [
        pytest.param(CreateMode, "Overwrit", id="create-typo"),
        pytest.param(CreateMode, "exists_ok", id="create-near-miss"),
        pytest.param(CreateMode, "exis_tok", id="create-misplaced-underscore"),
        pytest.param(CreateMode, " create", id="create-padded"),
        pytest.param(CreateMode, "Skip", id="create-word-from-another-set"),
        pytest.param(RegisterMode, "ExistOk", id="register-create-only-word"),
        pytest.param(InsertMode, "create", id="insert-pylance-only-word"),
        pytest.param(InsertMode, "Appnd", id="insert-typo"),
        pytest.param(DropMode, "PURGE", id="drop-query-param-in-the-body"),
        pytest.param(DropMode, "Overwrite", id="drop-word-from-another-set"),
        pytest.param(DropBehavior, "Cascde", id="behavior-typo"),
    ],
)
def test_a_value_outside_the_vocabulary_is_invalid_input_naming_it(vocabulary: type[StrEnum], raw: str) -> None:
    """Owner ruling 2026-09-25: refused as InvalidInput (spec code 13), never folded to the default.
    The message names the value as sent and every value the set does accept, so the caller can fix it."""
    with pytest.raises(InvalidInputError) as refused:
        _PARSERS[vocabulary](raw)

    assert refused.value.code == ErrorCode.INVALID_INPUT
    message = str(refused.value)
    assert repr(raw) in message, f"the refusal must name the value it refused: {message}"
    missing = [_pascal(member) for member in vocabulary if _pascal(member) not in message]
    assert not missing, f"the refusal must list every valid value, missing {missing}: {message}"
