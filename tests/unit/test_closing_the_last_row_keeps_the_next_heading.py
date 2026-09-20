"""Closing a row must not swallow the section heading that follows it.

MEASURED 2026-09-20 by doing it: `backlog_close.py LH-181` deleted
`## PHASE 1 · CROSS-CUTTING` along with the row, because LH-181 was the last item before that
heading. The register then held 74 cross-cutting rows attributed to the LAKEHOUSE phase, the
section table stopped matching, and `test_the_walk_sees_the_register` went red — which is how it was
noticed, by luck of a gate aimed at something else.

THE TWO READINGS OF THE FILE DISAGREED, which is the actual defect. `_blocks` cuts a row from its
header to the NEXT ROW HEADER (`_ITEM_START`), while `_section_bodies` one function below clamps the
same span with `min(end, next_section_heading)`. The module's own comment at `_SECTION` already
states the rule the cut was breaking — "that is how `test_the_backlog_counts_itself.py` attributes a
row, and the two readings have to be the same one".

IT IS A DATA-LOSS PATH, not a formatting one: whatever sits between the closed row and the next row
header is deleted with it, silently, and only `git diff` shows what went. A heading is the cheap
case; the expensive one is a heading plus whatever prose introduces the section under it.
"""

from __future__ import annotations

import importlib.util
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[2]


def _closer():
    """Load `scripts/backlog_close.py` by path — it is a script, not an importable package member."""
    spec = importlib.util.spec_from_file_location("backlog_close", ROOT / "scripts" / "backlog_close.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REGISTER = """# header

**AA-001 · first**
`svc` · **HIGH**
- *What is left:* nothing.

**AA-002 · last in its section**
`svc` · **HIGH**
- *What is left:* nothing.

## PHASE 1 · CROSS-CUTTING

**BB-001 · first of the next section**
`svc` · **HIGH**
- *What is left:* nothing.
"""


def test_a_row_in_the_MIDDLE_of_a_section_cuts_at_the_next_row() -> None:
    """The control. Without it the assertion below could pass on a cut that takes nothing at all."""
    blocks = {num: (start, end) for num, start, end in _closer()._blocks(_REGISTER)}
    start, end = blocks["AA-001"]

    assert "AA-002" not in _REGISTER[start:end], "a row's span reached into the next row"
    assert "first" in _REGISTER[start:end]


def test_the_LAST_row_of_a_section_stops_at_the_heading() -> None:
    """THE DEFECT: the span ran to the next ROW, so the heading between them was inside it and a close
    deleted it."""
    blocks = {num: (start, end) for num, start, end in _closer()._blocks(_REGISTER)}
    start, end = blocks["AA-002"]

    assert "## PHASE 1 · CROSS-CUTTING" not in _REGISTER[start:end], (
        f"closing the last row of a section would delete the heading that follows it — the span is {_REGISTER[start:end]!r}"
    )


def test_closing_that_row_leaves_the_heading_in_the_file() -> None:
    """The end-to-end shape, because a correct span is only useful if the writer uses it."""
    module = _closer()
    blocks = {num: (start, end) for num, start, end in module._blocks(_REGISTER)}
    start, end = blocks["AA-002"]
    remaining = _REGISTER[:start] + _REGISTER[end:]

    assert "## PHASE 1 · CROSS-CUTTING" in remaining
    assert "BB-001" in remaining
    assert "AA-002" not in remaining
