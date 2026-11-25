"""A script of `scripts/`, imported the way `python scripts/<name>.py` runs it, for a test of the script's own functions:
`scripts/` is no workspace member, so a test cannot import it as a package."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest


#: The scripts' directory, which `python scripts/<name>.py` puts first on the path, so a script imports its siblings.
SCRIPTS: Final = Path(__file__).resolve().parents[1] / "scripts"


def script(name: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """``scripts/<name>.py`` under its own name, for the length of the test.

    The scripts' directory goes first on the path, so the script resolves a sibling as it does when run, and the module is
    registered in ``sys.modules`` before it executes, as Python registers a script: a dataclass or a pydantic model
    resolves its postponed annotations through ``sys.modules[cls.__module__]``. ``monkeypatch`` undoes both at teardown.
    """
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"no script {name}"
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module
