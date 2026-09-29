"""catalog-api-16 — a constrained wire value is parsed ONCE, into an enum, not re-lowered per decision.

``create``'s ``mode`` arrives as a bare ``str | None`` and was re-derived four separate times against a
hand-written vocabulary: ``(mode or "").lower() not in ("existok", "exist_ok")`` for the compensation
rule, ``_mode = (mode or "").lower()`` plus two membership tests for the pre-existence guards,
``(mode or "").lower() in ("existok", "exist_ok")`` again for the schema read-back, and
``(mode or "create").lower()`` once more down in the data plane. Four copies of one vocabulary, on the
door where getting it wrong means an Overwrite that silently creates or an ExistOk that seizes
ownership. ``drop_namespace``'s ``behavior`` had the same shape.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

import pytest
from lance_namespace import ErrorCode, InvalidInputError

from catalog.core.modes import CreateMode, DropBehavior, DropMode, InsertMode, RegisterMode


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
@pytest.mark.parametrize("absent", [""], ids=["blank"])
def test_an_absent_value_is_the_spec_default(vocabulary: type[StrEnum], default: StrEnum, absent: str | None) -> None:
    """Absent is the spec's own "(default)"; blank is how an empty query parameter arrives."""
    assert _PARSERS[vocabulary](absent) is default


@pytest.mark.parametrize(
    ("vocabulary", "raw"),
    [
        pytest.param(CreateMode, "Overwrit", id="create-typo"),
        pytest.param(CreateMode, " create", id="create-padded"),
        pytest.param(RegisterMode, "ExistOk", id="register-create-only-word"),
        pytest.param(InsertMode, "create", id="insert-pylance-only-word"),
        pytest.param(DropMode, "PURGE", id="drop-query-param-in-the-body"),
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
