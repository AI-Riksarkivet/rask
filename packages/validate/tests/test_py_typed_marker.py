"""validate ships the PEP 561 marker its consumers need (PS-27).

Not one of the five shared packages carried a ``py.typed``, so every consumer installed a package
whose annotations a type checker is required to IGNORE — including the estate's own gate, which runs
with ``error-on-warning = true``. Every carefully written signature in here was invisible at the only
boundary it exists to protect. The marker is a file, and it has to sit inside the directory the wheel
target actually ships.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def test_the_package_ships_a_py_typed_marker() -> None:
    packages = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert packages, "the wheel target declares no package directory"
    for relative in packages:
        marker = _ROOT / relative / "py.typed"
        assert marker.exists(), f"{relative} ships no PEP 561 marker, so its annotations are ignored by every consumer"
        assert marker.read_text(encoding="utf-8").strip() == "", "the marker is a presence flag, not a config file"
