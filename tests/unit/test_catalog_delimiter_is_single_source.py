"""DUP-11: the catalog table-id delimiter (``$``) has ONE definition.

The delimiter was hand-typed as a bare ``"$"`` in seventeen places — five env-var ``Field`` defaults,
three ``os.getenv`` defaults, two module constants, several ``delimiter: str = "$"`` params, and the
parse sites that ``.split("$")`` / ``.partition("$")`` a governed table id. Every one must resolve
through ``service_kit.lakehouse.naming.CATALOG_DELIMITER`` so the id the catalog mints and the id every
producer/mover/reader parses cannot drift apart. This gate fails if a bare literal reappears in any of
those delimiter contexts.

Regex-embedded delimiters (``DATASET_PATTERN = r"...\\$..."``) are a separate concern and out of
scope — this scans only the split/partition/Field-default/getenv-default/constant contexts.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL = _REPO_ROOT / "packages" / "service-kit" / "src" / "service_kit" / "lakehouse" / "naming.py"

#: The delimiter contexts the finding names — a bare ``"$"`` in any of these is a duplicated literal.
_CONTEXTS = [
    re.compile(r'\.split\("\$"'),
    re.compile(r'\.partition\("\$"'),
    re.compile(r'Field\(default="\$"'),
    re.compile(r'delimiter: str = "\$"'),
    re.compile(r'getenv\([^)]*,\s*"\$"\)'),
    re.compile(r'DELIMITER\s*=\s*"\$"'),
    re.compile(r'"\$" in '),
]


def _source_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")]


def _source_modules() -> list[Path]:
    files: list[Path] = []
    for root in (_REPO_ROOT / "services", _REPO_ROOT / "packages"):
        for p in root.rglob("*.py"):
            if "tests" in p.parts or p.name.startswith("test_") or p == _CANONICAL:
                continue
            files.append(p)
    return files


def test_canonical_delimiter_exists() -> None:
    assert _CANONICAL.exists(), "service_kit.lakehouse.naming.CATALOG_DELIMITER must be the one definition"


def test_no_module_hand_types_the_delimiter_in_a_parse_or_default_context() -> None:
    offenders: list[str] = []
    for path in _source_modules():
        for ln in _source_lines(path):
            if any(pat.search(ln) for pat in _CONTEXTS):
                offenders.append(f"{path}: {ln.strip()}")
    assert not offenders, "route the delimiter through CATALOG_DELIMITER:\n" + "\n".join(offenders)
