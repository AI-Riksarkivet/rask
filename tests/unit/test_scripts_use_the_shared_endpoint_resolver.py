"""The S3 endpoint's env precedence is resolved in ONE place: `storage.configured_endpoint()`.

The canonical-first list is `RASK_S3_ENDPOINT_URL`, `S3_ENDPOINT_URL`, `HCP_ENDPOINT`, and
`storage.s3_client()` resolves it that way when handed no endpoint. A second copy in a script does
not fail loudly when it drifts — it fails by omitting an alias, and then reports "S3 is not
configured" for a deployment the very client it is about to build would have connected to. That is
the least debuggable shape a configuration bug has: a green-looking refusal.

So `scripts/` may READ the endpoint, but may not decide what the endpoint is.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest


_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"

# The names `storage.client._ENDPOINT_ENVS` owns. A script naming any of them in a `getenv` is
# resolving the endpoint itself; naming one in a docstring (a run recipe) is fine, which is why this
# reads calls rather than text.
_ENDPOINT_ENVS = ("RASK_S3_ENDPOINT_URL", "S3_ENDPOINT_URL", "HCP_ENDPOINT")


def _getenv_endpoint_sites(tree: ast.AST) -> list[tuple[int, str]]:
    """Every `os.getenv(<endpoint env>)` / `getenv(...)` / `os.environ[...]` / `.get(...)` site.

    BOTH CALLABLE SHAPES, and the bare one is not hypothetical: `from os import getenv` makes the
    call an `ast.Name`, so a walk that inspected only `ast.Attribute` scored zero hits on it and this
    gate reported green over the very read it exists to refuse. A regression gate with a spelling it
    cannot see is worse than no gate, because it gets quoted as proof.

    A bare Name matches `getenv` ALONE. `get` is matched only as an attribute (`environ.get`): a bare
    `get(...)` is an ordinary call, and matching it would make this fire on unrelated code until
    somebody deleted the gate to get their work through.
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        name: object = None
        lineno = 0
        if isinstance(node, ast.Call):
            lineno = node.lineno
            func = node.func
            if isinstance(func, ast.Attribute):
                callee = func.attr if func.attr in {"getenv", "get"} else None
            elif isinstance(func, ast.Name):
                callee = func.id if func.id == "getenv" else None
            else:
                callee = None
            if callee and node.args and isinstance(node.args[0], ast.Constant):
                name = node.args[0].value
        elif isinstance(node, ast.Subscript):
            lineno = node.lineno
            if isinstance(node.slice, ast.Constant):
                name = node.slice.value
        if isinstance(name, str) and name in _ENDPOINT_ENVS:
            hits.append((lineno, name))
    return hits


def test_no_script_resolves_the_s3_endpoint_by_hand() -> None:
    """A script asks `configured_endpoint()`; it does not re-derive the precedence list."""
    offenders: list[str] = []
    for path in sorted(_SCRIPTS.rglob("*.py")):
        for lineno, name in _getenv_endpoint_sites(ast.parse(path.read_text(), filename=str(path))):
            offenders.append(f"{path.relative_to(_REPO)}:{lineno} reads {name} directly")
    assert not offenders, "scripts must call storage.configured_endpoint():\n  " + "\n  ".join(offenders)


def test_no_shell_script_resolves_the_s3_endpoint_by_hand() -> None:
    """Python embedded in a `.sh` heredoc is still a second copy of the precedence list."""
    names = "|".join(_ENDPOINT_ENVS)
    pattern = re.compile(rf"""(?:getenv|environ(?:\.get)?)\s*[(\[]\s*['"](?:{names})['"]""")
    offenders: list[str] = []
    for path in sorted(_SCRIPTS.rglob("*.sh")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(_REPO)}:{lineno}")
    assert not offenders, "shell-embedded python must call storage.configured_endpoint():\n  " + "\n  ".join(offenders)


def _load(stem: str) -> ModuleType:
    """Load a script by PATH — `scripts/` is not a package, exactly as `test_seed_bronze_pages.py` does."""
    spec = importlib.util.spec_from_file_location(stem, _SCRIPTS / f"{stem}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_verify_lance_storage_runs_against_an_endpoint_only_the_shared_resolver_knows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`HCP_ENDPOINT` alone is a configured backend — the S3 rows must run, not report BLOCKED."""
    module = _load("verify_lance_storage")
    monkeypatch.delenv("RASK_S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("HCP_ENDPOINT", "http://localhost:9000")
    module._s3()  # noqa: SLF001 — the seam under test


def test_smoke_scripts_report_the_endpoint_the_client_will_actually_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """A smoke script's printed endpoint is the one `s3_client()` builds against, aliases included."""
    from storage import configured_endpoint

    monkeypatch.delenv("RASK_S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("HCP_ENDPOINT", "http://localhost:9000")
    resolved = configured_endpoint()
    for stem in ("smoke_s3", "smoke_rustfs"):
        source = (_SCRIPTS / f"{stem}.py").read_text()
        assert "configured_endpoint" in source, f"{stem}.py does not use the shared resolver"
    assert resolved == "http://localhost:9000"


def test_the_scan_also_sees_a_BARE_getenv_import() -> None:
    """`from os import getenv` is the evasion the first version of this gate could not see.

    Its walk inspected `ast.Attribute` callables only, so `os.getenv(...)` was caught and the bare
    `getenv(...)` — the same read, one import line apart — scored zero hits and the gate stayed green.
    A regression gate with a spelling it cannot see is worse than none, because it is quoted as proof.

    Only `getenv` is matched on a bare Name: a bare `get(...)` is an ordinary call, not an env read,
    and matching it would make the gate fire on unrelated code until someone deleted the gate.
    """
    evasion = ast.parse("from os import getenv\nendpoint = getenv('RASK_S3_ENDPOINT_URL')\n")
    assert [name for _, name in _getenv_endpoint_sites(evasion)] == ["RASK_S3_ENDPOINT_URL"]

    innocent = ast.parse("mapping = {}\nvalue = get('RASK_S3_ENDPOINT_URL')\n")
    assert _getenv_endpoint_sites(innocent) == [], "a bare get() is not an environment read"
