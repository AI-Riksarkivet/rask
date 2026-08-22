"""A test may not write real state into a FIXED `/tmp` path — it must take `tmp_path`.

Two fixtures pointed a real catalog at `/tmp/lance-test-root` and `/tmp/lance-user-state-test`.
`monkeypatch.setenv` restored the ENVIRONMENT on teardown, which reads as hygiene and is why this
survived; what it does not restore is the DIRECTORY. So real Lance datasets accumulated in one path
shared across every run on the host — across reruns, across two concurrent runs of the same suite, and
across users. The failure mode is the expensive kind: a test passes because of what a previous run left
behind, and the first person to see it fail is whoever runs the suite on a clean machine (CI).

WHY THIS PARSES INSTEAD OF GREPPING. The fixes left the old paths written down in the comments that
explain them, so a `grep '/tmp/'` gate fires on its own justification — and the cure for that is a
narrower pattern, which is how a gate ends up matching prose instead of code. This is the M8 class the
audit found live in `.dagger/charts.go`, where two chart gates were asserting over YAML COMMENTS and
passing on config that did not exist. A gate over code must assert over code, so this one walks the AST:
`#` comments never enter it, and docstrings are the one string form that does, so they are skipped
explicitly.

WHAT IT LOOKS FOR IS THE DEFECT, NOT THE SUBSTRING. A first cut flagged every `/tmp/...` literal and
caught eighteen inert ones — `"/tmp/from"` and `"/tmp/to"` handed to a MOCKED mover, `"/tmp/a.lance"` as
a URI in an assertion. None of those touch a filesystem, and exempting them one by one would have built
exactly the drifting allowlist this gate is supposed to make unnecessary. So the scan matches the three
forms that actually point a REAL service at a root — `monkeypatch.setenv("<...>_ROOT", ...)`, a
`{"root": ...}` settings mapping, and a `root=` keyword — which is precisely how both original defects
were written, and leaves a literal that is only ever compared against alone.
"""

import ast
import tomllib
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]

#: Files allowed to name a fixed `/tmp` path, each with the reason. Empty on purpose — see the docstring.
_EXEMPT: dict[str, str] = {}


def _testpaths() -> list[Path]:
    """The suite's own roots, read from `pyproject.toml` rather than restated here.

    Restating them is the bug this avoids: a new testpath would be added to the config and not to the
    gate, and the gate would keep passing while covering less of the estate every release.
    """
    config = tomllib.loads((_REPO / "pyproject.toml").read_text())
    paths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    return [_REPO / p for p in paths]


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Every string constant that is a docstring, by node id — the one string form comments cannot hide in."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            found.add(id(body[0].value))
    return found


def _fixed_tmp_roots(tree: ast.Module, docstrings: set[int]) -> list[tuple[int, str]]:
    """The three forms that root a real service at a path, each carrying a `/tmp` literal."""
    found: list[tuple[int, str]] = []

    def literal(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/tmp/") and id(node) not in docstrings:
            return node.value
        return None

    for node in ast.walk(tree):
        # monkeypatch.setenv("SOMETHING_ROOT", "/tmp/...")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "setenv" and len(node.args) == 2:
            name = node.args[0]
            if isinstance(name, ast.Constant) and isinstance(name.value, str) and name.value.endswith("_ROOT"):
                value = literal(node.args[1])
                if value:
                    found.append((node.lineno, f"setenv({name.value}, {value!r})"))
        # {"root": "/tmp/..."} — a settings mapping
        if isinstance(node, ast.Dict):
            for key, value_node in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "root":
                    value = literal(value_node)
                    if value:
                        found.append((node.lineno, f'{{"root": {value!r}}}'))
        # root="/tmp/..." — a settings keyword
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "root":
                    value = literal(kw.value)
                    if value:
                        found.append((node.lineno, f"root={value!r}"))
    return found


def _scan() -> tuple[list[str], int, int]:
    """Returns (offences, files scanned, string constants examined)."""
    offences: list[str] = []
    files = 0
    strings = 0
    for root in _testpaths():
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            files += 1
            tree = ast.parse(path.read_text())
            strings += sum(1 for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str))
            rel = path.relative_to(_REPO).as_posix()
            if rel in _EXEMPT or rel == Path(__file__).relative_to(_REPO).as_posix():
                continue
            for lineno, form in _fixed_tmp_roots(tree, _docstring_nodes(tree)):
                offences.append(f"{rel}:{lineno} -> {form}")
    return offences, files, strings


def test_no_test_writes_state_into_a_fixed_tmp_path() -> None:
    offences, _, _ = _scan()
    assert not offences, "a test names a fixed /tmp path; take the `tmp_path` fixture instead so each run gets its own directory:\n  " + "\n  ".join(offences)


def test_the_scan_actually_reaches_the_estate() -> None:
    """The non-vacuity half — a scan that silently stops finding files reports a clean estate.

    Both floors are DERIVED rather than remembered. The file count is checked against the same
    `rglob` the scan uses, so the two can only agree if the testpaths resolved; the string count is a
    floor low enough never to need touching and high enough that an empty parse cannot pass it. A
    hard-coded expected total would go stale on the next test added, and the usual repair for a stale
    floor is to lower it.
    """
    offences, files, strings = _scan()
    on_disk = sum(len(list(root.rglob("*.py"))) for root in _testpaths() if root.is_dir())

    assert files == on_disk, f"scanned {files} files but {on_disk} are on disk under the testpaths"
    assert files >= 200, f"only {files} test files reached — the testpaths did not resolve"
    assert strings >= 5_000, f"only {strings} string constants examined — the scan is not parsing bodies"
    assert isinstance(offences, list)


def test_the_gate_fails_on_a_planted_offence(tmp_path: Path) -> None:
    """Prove the detector fires — and that it ignores the inert forms — rather than trusting a green run.

    A gate only ever observed passing is indistinguishable from one whose pattern matches nothing, which
    is the defect class this audit keeps finding. The planted file carries all three offending forms plus
    the three shapes that must NOT trip it: a docstring, a bare literal, and a mover URI.
    """
    planted = tmp_path / "planted.py"
    planted.write_text(
        '''"""A docstring naming /tmp/should-be-ignored."""

monkeypatch.setenv("LANCE_REST_ROOT", "/tmp/lance-test-root")
SETTINGS = {"root": "/tmp/lance-user-state-test"}
s = Settings(root="/tmp/other")

INERT = "/tmp/from"
mover(source="/tmp/a.lance")
'''
    )
    tree = ast.parse(planted.read_text())
    caught = [form for _, form in _fixed_tmp_roots(tree, _docstring_nodes(tree))]
    assert caught == [
        "setenv(LANCE_REST_ROOT, '/tmp/lance-test-root')",
        "{\"root\": '/tmp/lance-user-state-test'}",
        "root='/tmp/other'",
    ], f"detector caught the wrong set: {caught}"


@pytest.mark.parametrize("name", sorted(_EXEMPT))
def test_every_exemption_still_names_a_real_file(name: str) -> None:
    assert (_REPO / name).is_file(), f"{name} is exempted but does not exist — delete the exemption"
