"""storage's own packaging metadata: requires-python floor (PS-06) and declared deps (PS-08)."""

import re
import tomllib
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _ROOT / "pyproject.toml"
_SRC = _ROOT / "src"


def _pyproject() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def _requires_python_floor() -> tuple[int, int]:
    spec = _pyproject()["project"]["requires-python"]
    match = re.search(r">=\s*(\d+)\.(\d+)", spec)
    assert match, f"cannot parse a floor out of requires-python = {spec!r}"
    return int(match.group(1)), int(match.group(2))


def _uses_pep695_type_alias() -> bool:
    """PEP 695 `type X = …` statements are a 3.12+ syntax feature."""
    return any(re.search(r"^type\s+\w+\s*=", path.read_text(encoding="utf-8"), re.MULTILINE) for path in _SRC.rglob("*.py"))


def test_requires_python_floor_covers_pep695_syntax():
    """A requires-python floor below 3.12 is a lie while the source uses PEP 695 aliases."""
    if not _uses_pep695_type_alias():
        return
    floor = _requires_python_floor()
    assert floor >= (3, 12), f"requires-python floor {floor} predates the PEP 695 `type` syntax the source uses"


# Distribution name → import module name for storage's runtime deps.
_IMPORT_NAMES = {"boto3": "boto3"}


def test_every_declared_dependency_is_imported():
    """A declared runtime dependency nothing imports is dead weight in the lock."""
    deps = _pyproject()["project"]["dependencies"]
    dist_names = [re.split(r"[<>=!~ \[]", dep, maxsplit=1)[0] for dep in deps]

    sources = "\n".join(path.read_text(encoding="utf-8") for path in (*_SRC.rglob("*.py"), *(_ROOT / "tests").rglob("*.py")))

    unused = []
    for dist in dist_names:
        module = _IMPORT_NAMES.get(dist)
        assert module, f"add {dist!r} to _IMPORT_NAMES so this guard can check it"
        if not re.search(rf"\b(?:import\s+{module}|from\s+{module})\b", sources):
            unused.append(dist)
    assert not unused, f"declared but never imported: {unused}"


# ── The narrative doc describes the package that exists ──────────────────────────────────────

_DOCS = _ROOT.parents[1] / "docs"


def test_the_narrative_doc_does_not_deny_the_source_sink_contract() -> None:
    """It said "There is **no base class** — Source/Sink is a duck-typed structural contract"."""
    page = (_DOCS / "packages" / "storage.md").read_text(encoding="utf-8")
    assert "no base class" not in page, "`storage.protocol` now states the contract, and the S3 pair shares one"
    assert "storage.protocol" in (_DOCS / "reference" / "storage.md").read_text(encoding="utf-8"), "a public module absent from the generated API reference"


def test_the_narrative_doc_does_not_advertise_a_module_that_moved_out() -> None:
    """`iiif.py` left for `runners/htr` on 2026-08-17; the page still tabled it as storage's."""
    page = (_DOCS / "packages" / "storage.md").read_text(encoding="utf-8")
    assert not (_SRC / "iiif.py").exists()
    assert "`iiif.py`" not in page, "the page still lists a module this package does not have"
