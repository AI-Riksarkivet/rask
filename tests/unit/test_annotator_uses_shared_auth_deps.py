"""DUP-13: the annotator's auth dependencies come from the shared ``make_auth_deps`` factory.

``service_kit.governed.deps`` was extracted FROM annotator (its module docstring says so), but the
annotator was never migrated onto it — ``annotator/api/security.py`` kept line-for-line copies of
``authenticate``, ``current_subject``, ``get_checker``, ``get_fga_client`` and the ``FgaChecker``
Protocol. When the shared contract widens, a stranded copy drifts. This gate fails if the annotator
re-grows a local body for any of the factory-provided dependencies.
"""

from __future__ import annotations

import ast
from pathlib import Path


_SECURITY = Path(__file__).resolve().parents[2] / "services" / "annotator" / "src" / "annotator" / "api" / "security.py"

#: Dependencies that make_auth_deps owns — the annotator must re-export, never redefine, these.
_FACTORY_OWNED = {"authenticate", "current_subject", "get_checker", "get_fga_client"}


def test_annotator_security_defines_no_local_auth_bodies() -> None:
    tree = ast.parse(_SECURITY.read_text())
    local = {node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in _FACTORY_OWNED}
    assert not local, f"annotator must import these from make_auth_deps, not redefine them: {sorted(local)}"


def test_annotator_deps_are_the_shared_closures() -> None:
    from annotator.api import security

    # The re-exported dependencies must be the closures make_auth_deps built — same identity is what
    # makes the routes' Depends() and the tests' dependency_overrides line up. make_auth_deps defines
    # them inside itself, so they carry its qualname; a hand-written local copy would not.
    assert "make_auth_deps" in security.authenticate.__qualname__
    assert "make_auth_deps" in security.current_subject.__qualname__
    assert "make_auth_deps" in security.get_checker.__qualname__
    assert "make_auth_deps" in security.get_fga_client.__qualname__
