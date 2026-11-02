"""The inbox serves no interactive API docs unless its deployment asks for them.

What leaks otherwise is a route table, parameter names and schemas, to anyone: docs and the OpenAPI schema are opt-in
across the estate (the lakehouse's own apps are gated in `tests/unit/test_docs_are_closed_by_default.py`). The app is
read in a clean process with every docs opt-in stripped from its environment, because it builds its settings at import:
what is measured is the default a deployment that sets nothing gets, and a shell with docs turned on cannot turn this
green.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


_PROBE = """
import json
from notifications import app
print(json.dumps({"openapi_url": app.openapi_url, "docs_url": app.docs_url, "redoc_url": app.redoc_url}))
"""


def test_the_inbox_serves_no_docs_by_default() -> None:
    env = {key: value for key, value in os.environ.items() if not (key.endswith("_DOCS") or key == "RASK_DOCS")}
    done = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, env=env, cwd=Path(__file__).resolve().parents[3], timeout=180, check=False
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {"openapi_url": None, "docs_url": None, "redoc_url": None}
