"""MED-015: `ray_submit.py` carried a comment narrating symbols that no longer exist.

The module-level block claimed `_TERMINAL_BAD` was "still live after A13: the TRAIN path reads it"
(with a line reference that pointed at an unrelated statement) — but the symbol, its sibling
`_TERMINAL_OK`, and the read all left the module when the re-attach decision moved into
`ray_kit.submit.submit_or_reattach(..., on_terminal_failure="report")`. A comment describing removed
code is worse than no comment: the next reader goes hunting for a constant the module does not define.

Source-hygiene pin: the dead names must not reappear in the module, in code OR in prose.
"""

from __future__ import annotations

from pathlib import Path


_RAY_SUBMIT = Path(__file__).resolve().parents[1] / "src" / "medallion" / "services" / "ray_submit.py"


def test_ray_submit_names_no_removed_terminal_constants() -> None:
    source = _RAY_SUBMIT.read_text()
    for dead in ("_TERMINAL_BAD", "_TERMINAL_OK"):
        assert dead not in source, (
            f"ray_submit.py mentions {dead}, a symbol deleted when the re-attach decision moved into "
            "ray_kit.submit.submit_or_reattach — a comment describing removed code must go with it"
        )
