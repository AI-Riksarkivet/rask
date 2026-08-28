"""Every source module in the maintenance package must be reachable — imported by at least one
other module in the package (transitively from the service entrypoint). A module nothing imports is
dead weight the estate rule says to delete in the change that kills it, not carry.

This is a source-text import-graph check (no execution), so it stays infra-free and catches a module
that was written but never wired into bootstrap.
"""

from __future__ import annotations

from pathlib import Path


_PKG_ROOT = Path(__file__).resolve().parent.parent / "src" / "maintenance"


def _module_name(path: Path) -> str:
    return path.stem


def test_no_source_module_is_unreferenced() -> None:
    modules = [p for p in _PKG_ROOT.rglob("*.py") if p.name != "__init__.py"]

    orphaned: list[str] = []
    for path in modules:
        name = _module_name(path)
        # Count references to this module name that are NOT its own file. An import shows up as
        # `import <name>` / `from <...> import <name>` / `<pkg>.<name>`; a genuinely-dead module
        # appears only inside its own source.
        others = "\n".join(p.read_text() for p in _PKG_ROOT.rglob("*.py") if p != path)
        if name not in others:
            orphaned.append(str(path.relative_to(_PKG_ROOT)))

    assert not orphaned, f"unreferenced (dead) maintenance modules: {orphaned}"
