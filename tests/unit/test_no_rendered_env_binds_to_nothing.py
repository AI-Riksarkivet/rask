"""Every namespaced env the chart renders onto a Python service binds to a settings field.

Q17-20's general form. An env var that binds to no setting is indistinguishable from a control when
read: three live stage runners carried `MEDALLION_RAY_S3_ACCESS_KEY_ID=rask-ray-compute`, and that one
string was quoted in three documents — `open_goal.md` and the backlog's own zero-trust row among them —
as the evidence that the Ray lane ran on a scoped storage identity. No service read it and no template
rendered it — it was residue of the approach `ray_submit.py` abandoned once the Ray Jobs API was found
to echo `runtime_env` back on an unauthenticated read. The identity was real, on the Ray POD; the
variable was decoration that three documents mistook for proof.

WHAT THIS CATCHES AND WHAT IT CANNOT. That instance was live drift, which no render-time gate can
see — Q17-17 is the row for drift. This closes the half that IS in the repo: a template that renders
`<PREFIX>_SOMETHING` no `BaseSettings` field accepts. `extra="ignore"` is why nothing else does —
pydantic-settings drops an unknown variable in silence, so the pod starts, the value is never read,
and `kubectl describe` shows a setting that is not one.

THE PREFIXES AND FIELDS ARE DISCOVERED, NOT LISTED. A hand-written list is the same failure one layer
up: the next service is the one nobody adds. The apps are the ones the render runs (each Python
container's uvicorn target names its module), their settings classes are every `BaseSettings` subclass
that exists once those modules are imported, the shared packages' included wherever a class lives, and
each class's `env_prefix` and `model_fields` are read off the class itself, so a new service is covered
by existing here rather than by an edit.

CHECKED AGAINST THE UNION OF EVERY CLASS, deliberately. `LANCE_` is the prefix of
the catalog, flows and notifications alike; asking whether one particular class binds a name would
accuse a variable that its own service reads perfectly well. The union under-reports and never
falsely accuses, which is the right trade for a gate whose whole subject is "binds to NOTHING".

THE WEB ZONES ARE OUT OF THIS GATE'S SCOPE. Their `LINEAGE_SERVICE_TOKEN_FILE`, `LINEAGE_API` and
`LANCE_GATEWAY_URL` are read by SvelteKit (`bff.ts`), a plane with no `BaseSettings` at all — which is why the gate names the Python plane in its title. The zone and runner
planes are checked by nothing here.
"""

from __future__ import annotations

import importlib
import pathlib
import pkgutil
import sys

from pydantic_settings import BaseSettings


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chart_render import containers, uvicorn_target  # noqa: E402
from test_invariants import _rendered_docs  # noqa: E402


#: Deployments whose env belongs to the JS plane, which binds it in `bff.ts` rather than in pydantic.
_JS_PLANE = "web-"


def _app_modules() -> dict[str, bool]:
    """The module of every uvicorn target the render runs (`catalog.main:app` -> `catalog.main`), and whether it builds
    its app when imported: a target uvicorn calls with `--factory` builds nothing until called."""
    found: dict[str, bool] = {}
    for _, _, container in containers(tuple(_rendered_docs())):
        if target := uvicorn_target(container):
            found[target.split(":", 1)[0]] = "--factory" not in [*(container.get("command") or []), *(container.get("args") or [])]
    return found


def _settings_classes() -> list[type[BaseSettings]]:
    """Every `BaseSettings` subclass loaded once each module of a rendered app's package is imported: the settings the
    running apps can read, a shared library's included through the modules that import it. A module that builds its app
    at import is not imported, and a module that will not import fails the gate rather than hiding the classes it
    defines."""
    apps = _app_modules()
    for package_name in sorted({module.split(".", 1)[0] for module in apps}):
        package = importlib.import_module(package_name)
        for info in pkgutil.walk_packages(getattr(package, "__path__", []), prefix=f"{package_name}."):
            if not apps.get(info.name, False):
                importlib.import_module(info.name)
    found: list[type[BaseSettings]] = []
    pending: list[type[BaseSettings]] = list(BaseSettings.__subclasses__())
    while pending:
        settings_cls = pending.pop()
        found.append(settings_cls)
        pending.extend(settings_cls.__subclasses__())
    return found


def _accepted_names() -> tuple[set[str], set[str]]:
    """`(the env prefixes the settings classes declare, every env name any of them accepts)`.

    A class with no `env_prefix` (lineage's `Signing`, whose fields carry their full env names as aliases)
    declares no prefix and contributes its names to the union.
    """
    prefixes: set[str] = set()
    names: set[str] = set()
    for settings_cls in _settings_classes():
        prefix = str(settings_cls.model_config.get("env_prefix") or "")
        if prefix:
            prefixes.add(prefix)
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
    assert prefixes, "no settings class with an env_prefix was found in any rendered package, so the gate would pass vacuously"
    return prefixes, names


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


def test_no_rendered_env_binds_to_nothing() -> None:
    """The gate. A `<PREFIX>_NAME` no settings class with that prefix accepts is read by nothing:
    `extra="ignore"` drops it in silence, so the pod is healthy and the setting is decoration."""
    prefixes, names = _accepted_names()
    unbound: dict[str, set[str]] = {}
    for deployment, env in _rendered_env():
        if env.startswith(tuple(prefixes)) and env not in names:
            unbound.setdefault(env, set()).add(deployment)

    assert not unbound, (
        "these rendered env vars bind to NO settings field, so nothing reads them and pydantic drops "
        "them silently — a value that looks like configuration and is not: " + "; ".join(f"{env} on {sorted(where)}" for env, where in sorted(unbound.items()))
    )


# --------------------------------------------------------------------------------------------- #
# THE OTHER TWO PLANES. The gate above can only ask a question pydantic can answer, so the zones and
# the sealed runners — which have no BaseSettings at all — were checked by nothing.
# --------------------------------------------------------------------------------------------- #
