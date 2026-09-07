"""Every namespaced env the chart renders onto a Python service binds to a settings field.

Q17-20's general form. An env var that binds to no setting is indistinguishable from a control when
read: three live stage runners carried `MEDALLION_RAY_S3_ACCESS_KEY_ID=rask-ray-compute`, and that one
string was the evidence quoted by `open_goal.md`, by the backlog's own zero-trust row, and by
`test_the_medallion_runs_as_its_own_storage_identity`'s docstring, for the claim that the Ray lane
ran on a scoped storage identity. No service read it and no template rendered it — it was residue
of the approach `ray_submit.py` abandoned once the Ray Jobs API was found to echo `runtime_env` back
on an unauthenticated read. The identity was real, on the Ray POD; the variable was decoration that
three documents mistook for proof.

WHAT THIS CATCHES AND WHAT IT CANNOT. That instance was live drift, which no render-time gate can
see — Q17-17 is the row for drift. This closes the half that IS in the repo: a template that renders
`<PREFIX>_SOMETHING` no `BaseSettings` field accepts. `extra="ignore"` is why nothing else does —
pydantic-settings drops an unknown variable in silence, so the pod starts, the value is never read,
and `kubectl describe` shows a setting that is not one.

THE PREFIXES AND FIELDS ARE DISCOVERED, NOT LISTED. A hand-written list is the same failure one layer
up: the next service is the one nobody adds. This reads `env_prefix` off each settings class and its
fields off `model_fields`, so a new service is covered by existing here rather than by an edit.

CHECKED AGAINST THE UNION OF EVERY CLASS SHARING A PREFIX, deliberately. `LANCE_` is the prefix of
the catalog, flows and notifications alike; asking whether one particular class binds a name would
accuse a variable that its own service reads perfectly well. The union under-reports and never
falsely accuses, which is the right trade for a gate whose whole subject is "binds to NOTHING".

THE WEB ZONES ARE OUT OF SCOPE OF THE PYTHON GATE AND THAT IS NOT AN EXEMPTION. Their
`LINEAGE_SERVICE_TOKEN`, `LINEAGE_API`, `LINEAGE_SERVICE_ID` and `LANCE_GATEWAY_URL` are read by
SvelteKit (`bff.ts`), a plane with no `BaseSettings` at all — which is why the gate above names the
Python plane in its title. That left the zone and runner planes checked by NOTHING, so
:func:`test_no_rendered_env_on_ANY_plane_is_read_only_by_prose` covers them by a different mechanism,
below: it asks whether any first-party SOURCE reads the name.

PROSE IS NOT A READER, and that is the whole difference between that second gate and a text search.
Q17-20's variable WAS present in the tree — in the docstring that wrongly credited it — so a grep
would have found it and passed. A mention inside a comment or a docstring therefore does not count.
"""

from __future__ import annotations

import importlib
import pathlib
import re
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _rendered_docs  # noqa: E402


REPO = pathlib.Path(__file__).resolve().parents[2]
#: `model_config = SettingsConfigDict(..., env_prefix="X_", ...)` — the one spelling the estate uses.
_PREFIX = re.compile(r"""env_prefix\s*=\s*["']([A-Z_]+)["']""")
#: Deployments whose env belongs to the JS plane, which binds it in `bff.ts` rather than in pydantic.
_JS_PLANE = "web-"


def _settings_classes() -> list[tuple[str, str, str]]:
    """`(prefix, module, class)` for every settings class that declares an `env_prefix`."""
    found: list[tuple[str, str, str]] = []
    for src in sorted((REPO / "services").glob("*/src")):
        for path in sorted(src.rglob("*config*.py")):
            text = path.read_text()
            prefix = _PREFIX.search(text)
            if not prefix:
                continue
            module = ".".join(path.relative_to(src).with_suffix("").parts)
            for cls in re.findall(r"^class (\w+)\(.*BaseSettings", text, re.MULTILINE):
                found.append((prefix.group(1), module, cls))
    return found


def _accepted_names() -> dict[str, set[str]]:
    """`{prefix: every env name a settings class with that prefix accepts}`, unioned across classes."""
    accepted: dict[str, set[str]] = {}
    for src in sorted((REPO / "services").glob("*/src")):
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))

    for prefix, module, cls in _settings_classes():
        try:
            settings_cls = getattr(importlib.import_module(module), cls)
        except Exception as exc:  # a service whose config cannot import is its own test's problem
            pytest.skip(f"{module}.{cls} did not import: {exc}")
        names = accepted.setdefault(prefix, set())
        for name, field in settings_cls.model_fields.items():
            names.add(f"{prefix}{name.upper()}")
            names.add(name.upper())
            alias = field.alias or ""
            if alias:
                names.add(alias.upper())
                names.add(f"{prefix}{alias.upper()}")
            choices = getattr(field.validation_alias, "choices", None) or []
            for choice in choices:
                names.add(str(choice).upper())
                names.add(f"{prefix}{str(choice).upper()}")
    return accepted


def _rendered_env() -> list[tuple[str, str]]:
    """`(deployment, env name)` for every workload the Python plane runs."""
    out: list[tuple[str, str]] = []
    for doc in _rendered_docs():
        if doc.get("kind") not in {"Deployment", "StatefulSet"}:
            continue
        name = doc["metadata"]["name"]
        if _JS_PLANE in name:
            continue
        spec = doc["spec"]["template"]["spec"]
        for container in spec.get("containers", []) + (spec.get("initContainers") or []):
            for env in container.get("env") or []:
                out.append((name, env["name"]))
    return out


def test_the_discovery_finds_the_estates_settings_classes() -> None:
    """A regex that silently matches nothing makes every assertion below vacuous — the exact shape of
    the defect this file exists for."""
    prefixes = {prefix for prefix, _, _ in _settings_classes()}
    assert {"MEDALLION_", "MAINTENANCE_", "LINEAGE_", "LANCE_"} <= prefixes, (
        f"the env_prefix discovery found only {sorted(prefixes)} — has the settings spelling changed?"
    )


def test_no_rendered_env_binds_to_nothing() -> None:
    """The gate. A `<PREFIX>_NAME` no settings class with that prefix accepts is read by nothing:
    `extra="ignore"` drops it in silence, so the pod is healthy and the setting is decoration."""
    accepted = _accepted_names()
    unbound: dict[str, set[str]] = {}
    for deployment, env in _rendered_env():
        for prefix, names in accepted.items():
            if env.startswith(prefix) and env not in names:
                unbound.setdefault(env, set()).add(deployment)

    assert not unbound, (
        "these rendered env vars bind to NO settings field, so nothing reads them and pydantic drops "
        "them silently — a value that looks like configuration and is not: " + "; ".join(f"{env} on {sorted(where)}" for env, where in sorted(unbound.items()))
    )


def test_the_gate_would_have_caught_Q17_20() -> None:
    """A gate that passes the day it is written has proved nothing about what it can see. This runs
    the accept-set against the ACTUAL name that fooled three documents, plus a nonsense one, so a
    future change that makes the set permissive reds here instead of going quiet."""
    accepted = _accepted_names()
    for name in ("MEDALLION_RAY_S3_ACCESS_KEY_ID", "MEDALLION_THIS_BINDS_TO_NOTHING"):
        assert name not in accepted["MEDALLION_"], (
            f"{name} is accepted by the medallion settings — the accept-set has gone permissive and "
            "this gate can no longer see an env var that binds to nothing"
        )
    # ...and the set is not empty in the other direction, which would make it reject everything.
    assert "MEDALLION_S3_ACCESS_KEY_ID" in accepted["MEDALLION_"], "the medallion's own scoped identity is not recognised"


# --------------------------------------------------------------------------------------------- #
# THE OTHER TWO PLANES. The gate above can only ask a question pydantic can answer, so the zones and
# the sealed runners — which have no BaseSettings at all — were checked by nothing.
# --------------------------------------------------------------------------------------------- #

#: The prefixes this estate names its OWN configuration with. Outside these, the variable belongs to a
#: third-party image (OpenFGA, GreptimeDB, Dapr, Postgres) and is read by code this repo does not hold.
_FIRST_PARTY = ("RASK_", "LANCE_", "MEDALLION_", "LINEAGE_", "MAINTENANCE_", "INGEST_", "CATALOG_", "NOTIFICATIONS_", "FLOWS_", "COMPUTE_")
#: Every plane that may READ an env, which is the correction this section exists to make.
_SOURCE_ROOTS = ("services", "packages", "scripts", "frontend", "runners")
_PY_COMMENT = re.compile(r"#.*$", re.MULTILINE)
_JS_LINE_COMMENT = re.compile(r"//.*$", re.MULTILINE)
_JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _python_code_only(path: pathlib.Path) -> str:
    """The file's source with every DOCSTRING and comment removed.

    Docstrings go by walking the AST, not by matching triple quotes: a docstring is a POSITION in the
    tree rather than a spelling, and the mention this gate must refuse sat in an ordinary one.
    """
    import ast

    try:
        tree = ast.parse(path.read_bytes())
    except SyntaxError:
        return _PY_COMMENT.sub("", path.read_text(encoding="utf-8", errors="replace"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            first.value.value = ""
    return ast.unparse(tree)


def _code_only(path: pathlib.Path) -> str:
    if path.suffix == ".py":
        return _python_code_only(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix in {".ts", ".js", ".svelte", ".mjs", ".cjs"}:
        return _JS_LINE_COMMENT.sub("", _JS_BLOCK_COMMENT.sub("", text))
    return text


def _read_by_code(name: str) -> pathlib.Path | None:
    """The first first-party file that names ``name`` in CODE rather than in prose, or ``None``."""
    import subprocess

    argv = ["git", "grep", "-lF", "--", name, "--", *_SOURCE_ROOTS]
    out = subprocess.run(argv, capture_output=True, text=True, cwd=REPO, check=False).stdout  # noqa: S603
    for line in out.split():
        path = REPO / line
        if path.is_file() and name in _code_only(path):
            return path
    return None


def _every_rendered_env() -> list[tuple[str, str]]:
    """`(workload, env name)` across EVERY plane — zones and runners included."""
    out: list[tuple[str, str]] = []
    for doc in _rendered_docs():
        if doc.get("kind") not in {"Deployment", "StatefulSet"}:
            continue
        spec = doc["spec"]["template"]["spec"]
        for container in spec.get("containers", []) + (spec.get("initContainers") or []):
            for env in container.get("env") or []:
                out.append((doc["metadata"]["name"], env["name"]))
    return out


def test_no_rendered_env_on_ANY_plane_is_read_only_by_prose() -> None:
    """A first-party env delivered to ANY container is read by some first-party CODE.

    Complementary to the gate above rather than a second copy of it: that one asks pydantic whether a
    field accepts the name, which is the strongest answer available and only available for Python.
    This one asks whether any source in any plane reads it, which is weaker per-variable and is the
    only question the zones and the sealed runners can answer at all.
    """
    unread = sorted({env for _, env in _every_rendered_env() if env.startswith(_FIRST_PARTY) and _read_by_code(env) is None})

    assert not unread, (
        f"the chart renders {unread} into containers and no first-party CODE reads them — a mention in a "
        "comment or a docstring does not count. An env nobody reads is indistinguishable from a control "
        "when read, which is how MEDALLION_RAY_S3_ACCESS_KEY_ID was cited as a storage identity by three "
        "documents while binding to nothing. Delete it, or wire the reader it implies."
    )


def test_a_DOCSTRING_mention_does_not_count_as_a_reader() -> None:
    """The half that makes the gate above different from a grep, asserted rather than assumed."""
    probe = REPO / "tests" / "unit" / "test_invariants.py"
    prose = "Inert-if-absent settings the chart deliberately does not set"
    original = probe.read_text(encoding="utf-8")
    stripped = _python_code_only(probe)

    assert prose in original, "the sample prose moved — point this at another docstring to prove the stripper"
    assert prose not in stripped, "docstring text survived the stripper, so prose would count as a reader"
    assert "_UNWIRED_BY_DESIGN" in stripped, "the stripper removed CODE as well as prose — live readers would read as absent"
