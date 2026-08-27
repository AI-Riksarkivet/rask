"""`import service_kit` must not require the optional Lance extras.

WHY. `service-kit` is shared by every service, including the storeless ones — the estate's rule is
that the heavy Lance/Ray deps stay behind `[governed]` / `[lakehouse]` / `[lancekit]` and the base
stays light. Two module-scope imports quietly broke that: `service_kit/__init__.py` imported
`lakehouse.ns_errors`, and `service_kit.body_limit` — middleware EVERY app gets — imported
`lance_namespace.ErrorCode` and `ns_errors.problem_body`.

The consequence was a deployable that could not be built at all. `services/gateway` declares
`service-kit` bare, so its image failed the build-time import gate with
`ModuleNotFoundError: No module named 'lance_namespace'`, while every test stayed green — the
workspace venv resolves the extra through a sibling member, so the gap is structurally invisible to
anything running in it. Five other bare consumers resolved it TRANSITIVELY, by luck.

These tests are the in-venv half. They cannot prove the extra is absent (it is present here), so they
assert the two properties that are checkable: no module in the base import path names
`lance_namespace` at module scope, and the one wire constant that had to be hard-coded to break the
cycle still equals the enum it stands for.
"""

from __future__ import annotations

import importlib
import sys

import pytest


#: Modules the BASE import path must not need. `service_kit.lakehouse` is the optional layer and may
#: import what it likes; so may a module nothing in `__init__` reaches (`control_emit`, imported only
#: by services that emit control events and therefore hold the extra). The rule is about the CLOSURE
#: of `import service_kit`, not about every file in the directory — which is why the first test below
#: simulates the absence rather than grepping for it.
_OPTIONAL = "lance_namespace"


def test_importing_service_kit_does_not_need_the_lance_extras() -> None:
    """The property itself, simulated: make `lance_namespace` unimportable and import the package.

    Stronger than any static scan, because it follows the real closure rather than a guess at it — and
    because it is exactly the situation the gateway's image is in. A `finally` restores `sys.modules`
    so this cannot leak into another test.
    """
    blocked = _BlockImport(_OPTIONAL)
    saved = {name: mod for name, mod in sys.modules.items() if name.split(".")[0] in {"service_kit", _OPTIONAL}}
    for name in saved:
        del sys.modules[name]
    sys.meta_path.insert(0, blocked)
    try:
        importlib.import_module("service_kit")
    except ModuleNotFoundError as exc:  # pragma: no cover - the failure path IS the finding
        pytest.fail(
            f"`import service_kit` needs `{exc.name}` when the extras are absent. That is how the "
            "gateway image stopped building: it declares `service-kit` bare, so its own dependency "
            "closure has no Lance packages at all. Defer the import into the function that needs it, "
            "or move the Lance-free part into `service_kit.problem`."
        )
    finally:
        sys.meta_path.remove(blocked)
        for name in [n for n in sys.modules if n.split(".")[0] in {"service_kit", _OPTIONAL}]:
            del sys.modules[name]
        sys.modules.update(saved)


class _BlockImport:
    """A meta-path finder that makes ONE top-level package unimportable, as an absent extra would."""

    def __init__(self, blocked: str) -> None:
        self.blocked = blocked

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if fullname == self.blocked or fullname.startswith(f"{self.blocked}."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return


def test_the_body_limit_code_still_matches_the_lance_namespace_enum() -> None:
    """The hard-coded wire constant must not drift from the enum it stands for.

    `body_limit` cannot import `ErrorCode` — that is the whole point — so it spells the number. This
    test runs where the extra IS installed and is the only thing standing between that literal and a
    silent divergence from the spec's own code.
    """
    lance_namespace = pytest.importorskip("lance_namespace", reason="the [lakehouse] extra is absent, so there is no enum to compare against")
    from service_kit.body_limit import _INVALID_INPUT

    assert int(lance_namespace.ErrorCode.INVALID_INPUT) == _INVALID_INPUT, (
        f"body_limit spells INVALID_INPUT as {_INVALID_INPUT}, but the Lance-Namespace enum now says "
        f"{int(lance_namespace.ErrorCode.INVALID_INPUT)} — a body-too-large response is quoting the wrong "
        "spec code to every client that dispatches on it."
    )
