"""The estate has ONE authorization model, so it must have ONE writer.

`fga.provision` calls `write_authorization_model` unconditionally, and OpenFGA mints a new immutable
model id for every call. `fga.resolve` then returns `found[0].id` under a docstring saying
`read_authorization_models` answers newest-first — so the estate's authoritative model is decided BY
RECENCY, not by version, and `chart/values.yaml` ships `fgaModelId: ""` (unpinned) as the default.

MEASURED ON THE LIVE ESTATE 2026-09-09, which is why this is a gate and not a preference:

  * the store held **1,256** authorization models — one per service boot since 2026-07-29;
  * the OLDEST carries no `pass_grants`, no `managed_access` and no `maintainer` — it predates the
    whole grant-delegation lattice;
  * and three services ran months-old images whose vendored `model.fga` hashes `9fe967cab6f5` with
    `maintainer: False`, against HEAD's `cb19b54faebb` with `maintainer: True`.

So one restart of `compute`, `flows` or `controlplane` after the catalog would publish a model with no
`maintainer` rung and make it the newest — silently withdrawing the rung the maintenance sweep's
write-tier credential vend depends on (`endpoints/credentials.py` accepts "can_write_data or
can_maintain"). Nothing would go red: every service would keep answering, against a weaker model.

`build_fga_client`'s own docstring already argues the fix — "`provision=False` takes the read-only
half … `None` back from it means the estate is not bootstrapped … which is the honest answer". This
pins that: a new service is read-only unless someone deliberately makes it the bootstrap.
"""

from __future__ import annotations

import ast
import pathlib


REPO = pathlib.Path(__file__).resolve().parents[2]

#: The ONE service permitted to publish. The catalog owns the governed plane — it is where the model
#: file's consumers live, it is `fatal=True` so a failure to build is a refusal to serve, and the
#: chart's bootstrap-admin hook already seeds the first tuples against its objects.
_BOOTSTRAP = "catalog"


def _provisioning_call_sites() -> dict[str, str]:
    """Every `attach_auth(...)`/`build_fga_client(...)` that asks to WRITE the model, by file."""
    found: dict[str, str] = {}
    for path in sorted((REPO / "services").rglob("*.py")) + sorted((REPO / "packages/service-kit/src").rglob("*.py")):
        if "/tests/" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in {"attach_auth", "build_fga_client"}:
                continue
            explicit = next((kw for kw in node.keywords if kw.arg == "provision"), None)
            # No keyword means the DEFAULT decides — which is exactly how nine services became
            # publishers without anyone choosing it. So the default is READ, never assumed: this gate
            # has to keep answering correctly whichever way `build_fga_client` is written.
            if explicit is None:
                if not _default_is_read_only():
                    found[str(path.relative_to(REPO))] = "default (which publishes)"
            elif isinstance(explicit.value, ast.Constant) and explicit.value.value is True:
                found[str(path.relative_to(REPO))] = "provision=True"
    return found


def _default_is_read_only() -> bool:
    src = (REPO / "packages/service-kit/src/service_kit/governed/auth_lifespan.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == "build_fga_client":
            for name, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
                if name.arg == "provision":
                    return isinstance(default, ast.Constant) and default.value is False
    raise AssertionError("build_fga_client has no `provision` keyword — this gate is reading the wrong seam")


def test_the_default_is_READ_ONLY() -> None:
    """A service that says nothing must not become a publisher of the estate's security model."""
    assert _default_is_read_only(), (
        "build_fga_client defaults provision=True, so every call site that does not opt out publishes a "
        "new authorization model on every boot and the newest one wins by recency"
    )


def test_exactly_ONE_call_site_publishes() -> None:
    publishers = _provisioning_call_sites()
    stray = {path: how for path, how in publishers.items() if f"/{_BOOTSTRAP}/" not in path}
    assert not stray, (
        f"only {_BOOTSTRAP} may publish the authorization model; these also do: {stray}. "
        "Two publishers means the estate's authoritative model is decided by boot order."
    )
    assert publishers, f"nothing publishes the model at all — {_BOOTSTRAP} must opt in explicitly"
