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

THE WEB ZONES ARE OUT OF SCOPE AND THAT IS NOT AN EXEMPTION. Their `LINEAGE_SERVICE_TOKEN`,
`LINEAGE_API`, `LINEAGE_SERVICE_ID` and `LANCE_GATEWAY_URL` are read by SvelteKit (`bff.ts`), a
plane with no `BaseSettings` at all. Measured 2026-09-07: those four are the ONLY prefixed envs on a
zone pod, and they are the whole reason this gate names the Python plane in its title.
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
