"""SK-21 — service-kit still pointed readers at two packages that no longer exist.

`packages/common` was deleted at the end of gate 3 (R19); `packages/ratch` was DISSOLVED by owner
ruling on 2026-08-28. Both were still named across this library as if they were live: TRANSITIONAL
markers promising a gate-5 port "onto packages/lineage-kit" that has already happened, an
`_APP_LOGGERS` allowlist raising the log level of two dead trees, a module docstring telling a reader
the Stage-aware half "lives up in ratch.lineage", and — the one that actively misleads — a claim that
`.docker/ray-cluster.dockerfile` installs `--package ratch`, which that dockerfile's own comment
records was replaced by `ray-cluster-env` at the dissolution.

A stale reference is not cosmetic here: it is the map somebody follows when deciding where a change
belongs, and every one of these sends them to a directory that is not there.
"""

from __future__ import annotations

import re
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src" / "service_kit"

#: The dissolved/deleted packages, as a reader would meet them: `ratch` as a word, `common.<module>`
#: as an import path, and the marker comments that promised a port already made.
_DEAD = {
    "ratch": re.compile(r"\bratch\b"),
    "common.<module> import path": re.compile(r"\bfrom common\b|\bcommon\.(lancekit|openlineage)\b"),
    "TRANSITIONAL gate marker": re.compile(r"TRANSITIONAL"),
}


def _sources() -> list[Path]:
    # `governed/` carries a generated .fga model whose sample objects are not prose; only .py here.
    return sorted(SRC.rglob("*.py"))


def test_no_module_names_a_dissolved_package() -> None:
    offenders: list[str] = []
    for path in _sources():
        text = path.read_text()
        for label, pattern in _DEAD.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(SRC)}:{line} names {label}")
    assert offenders == [], "service-kit points readers at packages that no longer exist:\n" + "\n".join(offenders)


def test_the_log_allowlist_names_only_packages_that_exist() -> None:
    """`_APP_LOGGERS` raises a package logger to INFO. An entry naming nothing raises nothing, and it
    is the same dead-rename shape that muted the maintenance sweep for the life of that service."""
    repo = SRC.parents[3]
    listed = set(re.findall(r'^\s*"([a-z_]+)",', (SRC / "obs.py").read_text(), re.MULTILINE))
    assert listed, "could not parse _APP_LOGGERS — the guard must fail loudly, not vacuously"
    existing = {p.name for p in repo.glob("services/*/src/*") if p.is_dir()} | {p.name for p in repo.glob("packages/*/src/*") if p.is_dir()}
    assert sorted(listed - existing) == []
