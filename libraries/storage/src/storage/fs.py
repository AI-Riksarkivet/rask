"""Filesystem source and sink (read-only iteration + writes)."""

from collections.abc import Iterable
from pathlib import Path


class FSSource:
    """Filesystem source: yields file paths under `root` matching any of `suffixes`."""

    def __init__(self, root: Path, suffixes: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".tif", ".tiff")):
        self.root = Path(root)
        self.suffixes = tuple(s.lower() for s in suffixes)

    def keys(self) -> Iterable[str]:
        for path in self.root.rglob("*"):
            if path.is_file() and any(path.name.lower().endswith(s) for s in self.suffixes):
                yield str(path.relative_to(self.root))

    def read(self, key: str) -> bytes:
        return (self.root / key).read_bytes()


class FSSink:
    """Filesystem sink: writes files under `root` (creates parent dirs as needed)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def existing_keys(self, suffix: str = "") -> Iterable[str]:
        for path in self.root.rglob("*"):
            if path.is_file() and (not suffix or path.name.lower().endswith(suffix.lower())):
                yield str(path.relative_to(self.root))

    def write(self, key: str, data: bytes) -> None:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
