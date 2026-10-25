"""The annotator's live suites, run against a deployed estate the annotator's release is installed beside.

They live with the client, so deleting the annotator deletes them (XC-183), and they drive the lakehouse through the same
live harness the lakehouse's own suites use: `tests/e2e-py`'s flat helpers (`byo_scenario`, `scratch_tenant`,
`correctness`) are put on `sys.path` here, and a run names `tests/e2e-py` beside this directory so its conftest registers
`--require-live` (`make e2e-annotator`, `scripts/e2e_stack.sh`). Offline runs deselect them by their `e2e` marker.
"""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tests" / "e2e-py"))
