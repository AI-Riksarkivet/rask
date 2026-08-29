"""Zero-cost stdlib modules are imported at module top, not inside functions (ANN-19).

The annotator's lazy-import convention exists for imports that COST something at import time — a
Dapr/ActorProxy channel open, pyarrow, an optional SDK. `json` and `re` cost nothing, so a
function-local `import json  # noqa: PLC0415` buys no start-up and hides the module's real
dependency surface while suppressing the very lint that would say so.
"""

from __future__ import annotations

import re
from pathlib import Path

import annotator


#: Stdlib modules whose import is effectively free — no justification exists for deferring them.
_ZERO_COST = ("json", "re", "os", "time", "asyncio")


def test_no_function_local_zero_cost_stdlib_imports() -> None:
    root = Path(annotator.__file__).parent
    # Horizontal whitespace only: under MULTILINE, `\s+` would swallow the newline and flag a
    # module-TOP import that merely follows a blank line.
    pattern = re.compile(rf"^[ \t]+import ({'|'.join(_ZERO_COST)})\b", re.MULTILINE)
    offenders = [f"{path.relative_to(root)}: {match.group(0).strip()}" for path in sorted(root.rglob("*.py")) for match in pattern.finditer(path.read_text())]
    assert offenders == [], f"function-local zero-cost stdlib imports — hoist to module top: {offenders}"
