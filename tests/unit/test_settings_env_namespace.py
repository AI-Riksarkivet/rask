"""Every fleet setting is reachable ONLY through its declared, namespaced env var.

`SettingsConfigDict(populate_by_name=True)` is set on six settings classes, and it is wanted: it is
what lets a test write ``MedallionSettings.model_validate({"ray_enabled": True})`` instead of
spelling out ``MEDALLION_RAY_ENABLED``. What is NOT wanted, and what it also does, is teach
pydantic-settings' env source a SECOND lookup name for every field -- the bare field name. So a field
declared ``ray_address: str = Field(alias="MEDALLION_RAY_ADDRESS")`` silently answers to
``RAY_ADDRESS`` as well, and ``RAY_ADDRESS`` is Ray's OWN standard environment variable.

That is not hypothetical. It was found because a session-scoped fixture in `packages/ratch/tests`
sets ``RAY_ADDRESS=local`` for its own Ray connection and never restores it, and five tests in two
other suites began failing with ``unknown url type: 'local/api/jobs/'`` -- the medallion's Ray
dashboard client, pointed at the string ``local``, because a bare env var it never declared had
overridden the one it did. The suite made it visible; a Ray sidecar, an operator, or a shell export
does the same thing to a running pod, where nothing is watching.

The rule this pins: **a settings field is settable by its declared alias and by nothing else.** Both
halves are asserted, because a "fix" that broke env loading outright would satisfy the second half
alone.

The fix, when this fires, is `env_prefix=` on the class -- NOT `populate_by_name=False`, which closes
the hole but also makes ``model_validate({"field_name": ...})`` *silently return the default* rather
than raise. `env_prefix` redirects the bare-name fallback onto the namespaced name the alias already
declares, so the fallback becomes harmless instead of absent.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from pydantic import AliasChoices
from pydantic_settings import BaseSettings


_ROOT = Path(__file__).resolve().parents[2]

#: EVERY `BaseSettings` subclass in the estate, as (src root, module, class). Explicit rather than
#: import-walked: a settings class that will not import without its service's deps would turn a
#: discovery walk into a skip, and a skipping gate measures nothing. Completeness is guarded instead,
#: by `test_the_roster_names_every_settings_class_in_the_estate` below -- so adding a settings class
#: without adding it here fails, rather than silently going unchecked.
#:
#: Only nine of these ever carried the defect (the ones declaring `populate_by_name`). The other eight
#: are here anyway: they cost milliseconds, and they are the ones a future edit would break silently.
_SETTINGS: list[tuple[str, str, str]] = [
    ("packages/service-kit", "service_kit.config", "Settings"),
    # `MediaSettings`, renamed from a second class called `Settings` (SK-10) — one distribution held
    # two of that name, on `RASK_*` and `MEDIA_*` respectively. `service_kit.media.config.Settings`
    # survives as an alias for the three services that import it, so this roster names the DEFINITION.
    ("packages/service-kit", "service_kit.media.config", "MediaSettings"),
    # The Dapr-delivered door's environment (SKG-10). `APP_API_TOKEN` is deliberately unprefixed —
    # daprd injects that exact name from `dapr.io/app-token-secret`, so it IS the declared name and
    # the bare-name rule below skips it rather than flagging it.
    ("packages/service-kit", "service_kit.governed.dapr_auth", "DaprDoorSettings"),
    ("packages/lineage-kit", "lineage_kit.config", "LineageSettings"),
    ("packages/ray-kit", "ray_kit.auth", "RayAuthSettings"),
    # ratch's two rows (JobsSettings RATCH_*, RunnersSettings) died with the package at the
    # dissolution (2026-08-28, open_ray-kernel.md); the roster-completeness test below is what
    # forces this list to shrink WITH the estate rather than accreting ghosts.
    ("services/lineage", "lineage.core.config", "LineageSettings"),
    ("services/medallion", "medallion.core.config", "MedallionSettings"),
    ("services/notifications", "notifications.api.settings", "IngressSettings"),
    ("services/catalog", "catalog.core.config", "Settings"),
    ("services/maintenance", "maintenance.core.config", "MaintenanceSettings"),
    # These three reach `populate_by_name` by declaring it next to a GovernedAuthSettings mixin, which
    # is why grepping for the flag on a `class X(BaseSettings)` line misses them.
    ("services/notifications", "notifications.config", "NotificationsSettings"),
    ("services/flows", "flows.config", "FlowsSettings"),
    ("services/ingest", "ingest.auth", "IngestAuthSettings"),
    # The OPERATIONAL half of the same service — every `RASK_INGEST_*` / upstream-address knob, moved
    # off 44 scattered `os.getenv` reads (ING-07). No `populate_by_name`, so the alias half of this
    # gate is the whole of its declaration.
    ("services/ingest", "ingest.config", "IngestSettings"),
    # The two services the gateway publishes that had NO auth code path at all until 2026-08-26 —
    # so no settings class either, and nothing for this roster to check. `ComputeSettings` subclasses
    # the shared `Settings` (it reads ray_dashboard_url and the rest of the common surface);
    # `ControlplaneSettings` is a bare BaseSettings + the mixin, since it reads none of it.
    ("services/compute", "compute.config", "ComputeSettings"),
    ("services/controlplane", "controlplane.config", "ControlplaneSettings"),
    # The FRONT DOOR, which had no settings class at all until FLEET-ENV-SCATTER: sixteen raw
    # `os.environ.get` reads, several of them per request. Neither `populate_by_name` nor
    # `env_prefix` — every field carries the deployed variable's full name as an explicit alias, so
    # the alias half of this gate is the whole of its declaration.
    ("services/gateway", "gateway.config", "GatewaySettings"),
    # Subclasses of service_kit.media.config.Settings. None declares `populate_by_name`, and
    # GovernedAuthSettings is a plain mixin rather than a BaseSettings, so none inherited it.
    ("services/search", "search.core.config", "SearchSettings"),
    ("services/viewer", "viewer.core.config", "ViewerSettings"),
    ("services/annotator", "annotator.core.config", "AnnotatorSettings"),
]

_IDS = [f"{m.split('.')[0]}.{c}" for _, m, c in _SETTINGS]

#: Fields with no default need a value before the class will construct at all.
_REQUIRED: dict[str, str] = {
    "LANCE_S3_ACCESS_KEY_ID": "x",
    "LANCE_S3_SECRET_ACCESS_KEY": "x",
    "MEDIA_S3_ACCESS_KEY_ID": "x",
    "MEDIA_S3_SECRET_ACCESS_KEY": "x",
}

_SENTINEL = "sentinel-value-no-field-would-default-to"


def _load(src: str, module: str, cls: str) -> type[BaseSettings]:
    path = str(_ROOT / src / "src")
    if path not in sys.path:
        sys.path.insert(0, path)
    return getattr(importlib.import_module(module), cls)


def _declared(cls: type[BaseSettings], field_name: str, field: Any) -> set[str]:
    """The env names this field is DECLARED to answer to, upper-cased.

    Two declaration styles are in use and both are legitimate. An explicit `alias=` names the variable
    outright (the fleet's `LANCE_*`/`MEDALLION_*` classes). Otherwise `env_prefix` + the field name IS
    the declaration (`ratch`'s `RATCH_*`, `lineage_kit`'s `RASK_LINEAGE_*`). Reading only the first
    style left the prefix-style classes with an empty declared set, which made the alias half of this
    gate silently exercise nothing on them.
    """
    alias = field.validation_alias or field.alias
    if alias is None:
        return {f"{cls.model_config.get('env_prefix', '')}{field_name}".upper()}
    if isinstance(alias, AliasChoices):
        return {str(c).upper() for c in alias.choices}
    return {str(alias).upper()}


def _build(cls: type[BaseSettings], env: dict[str, str]) -> Any:
    """Construct under EXACTLY `env` (plus what the class needs), or return the exception."""
    with mock.patch.dict(os.environ, {**_REQUIRED, **env}, clear=True):
        try:
            return cls()
        except Exception as exc:  # a value that will not coerce still proves the var was READ
            return exc


def _reads(cls: type[BaseSettings], name: str, baseline: Any, field_name: str) -> bool:
    """Does setting env var `name` change what `cls` resolves for `field_name`?"""
    got = _build(cls, {name: _SENTINEL})
    if isinstance(got, Exception):
        # The baseline constructs cleanly under the same env minus this one variable, so an exception
        # here is attributable to it: the class READ it and refused the value. Do not look for the
        # sentinel in the message -- pydantic-settings' SettingsError for a complex field ("error
        # parsing value for field ...") names the field and omits the value, which read as "the alias
        # is ignored" for the four list/dict fields in the fleet.
        return True
    return getattr(got, field_name) != getattr(baseline, field_name)


@pytest.mark.parametrize(("src", "module", "cls"), _SETTINGS, ids=_IDS)
def test_no_setting_answers_to_a_bare_un_namespaced_env_var(src: str, module: str, cls: str) -> None:
    settings_cls = _load(src, module, cls)
    baseline = _build(settings_cls, {})
    assert not isinstance(baseline, Exception), f"{cls} will not construct with a clean env: {baseline}"

    leaks: list[str] = []
    for field_name, field in settings_cls.model_fields.items():
        declared = _declared(settings_cls, field_name, field)
        bare = field_name.upper()
        if bare in declared:
            continue  # the bare name is the declared name; that is the whole contract
        if _reads(settings_cls, bare, baseline, field_name):
            leaks.append(f"{field_name} declares {sorted(declared)} but also answers to ${bare}")

    assert not leaks, (
        f"{cls} has {len(leaks)} field(s) settable through an env var they never declare.\n"
        + "\n".join(f"  - {leak}" for leak in leaks[:12])
        + (f"\n  ... and {len(leaks) - 12} more" if len(leaks) > 12 else "")
        + "\nFix with env_prefix= on the class (NOT populate_by_name=False -- that also breaks"
        " model_validate by field name, silently)."
    )


@pytest.mark.parametrize(("src", "module", "cls"), _SETTINGS, ids=_IDS)
def test_every_declared_alias_still_loads(src: str, module: str, cls: str) -> None:
    """The other half: closing the bare name must not close the declared one.

    LIMIT, stated because the name would otherwise overclaim: this reads each alias OUT of the model,
    so it proves a class honours whatever alias it declares -- NOT that the alias is the name the
    deployment actually sets. Renaming `MEDALLION_RAY_ADDRESS` to `MEDALLION_RAY_ADDRESS_TYPO` passes
    here, measured. That direction needs an external oracle and the chart is it
    (`test_invariants.py::test_no_dead_chart_env_vars`) -- but that oracle did not catch this rename
    either until it was fixed in the same change: it substring-matched the chart's env name against
    first-party source, and `MEDALLION_RAY_ADDRESS` is a substring of `MEDALLION_RAY_ADDRESS_TYPO`.
    It now requires the name not to run on into another identifier character.
    """
    settings_cls = _load(src, module, cls)
    baseline = _build(settings_cls, {})
    assert not isinstance(baseline, Exception)

    # Every field, not just the `str` ones: a field that refuses to coerce the sentinel still proves
    # it READ the variable, and `_reads` treats that refusal as a hit. Filtering to `str` left
    # IngestAuthSettings exercising two aliases out of fourteen.
    checked = 0
    for field_name, field in settings_cls.model_fields.items():
        for name in _declared(settings_cls, field_name, field):
            if name in _REQUIRED:
                continue
            assert _reads(settings_cls, name, baseline, field_name), f"{cls}.{field_name} ignores its own declared ${name}"
            checked += 1
    expected = sum(len(_declared(settings_cls, n, f) - set(_REQUIRED)) for n, f in settings_cls.model_fields.items())
    assert checked == expected, f"{cls}: exercised {checked} declared names but the class has {expected}"
    assert checked >= 1, f"{cls}: no declared name exercised -- this gate is not measuring anything"


def _discovered_settings_classes() -> set[tuple[str, str]]:
    """Every settings class in the Python planes, as (repo-relative path, class name).

    AST, not import: a class whose service deps are absent must still be DISCOVERED, or the roster
    could be short by exactly the class nobody can load. Follows inheritance by name to a fixpoint, so
    `SearchSettings(Settings)` is found even though `BaseSettings` is two hops away.
    """
    import ast

    by_file: dict[tuple[str, str], set[str]] = {}
    for plane in ("packages", "services"):
        for path in (_ROOT / plane).rglob("*.py"):
            if "/tests/" in str(path) or "/.venv/" in str(path):
                continue
            try:
                tree = ast.parse(path.read_text(errors="ignore"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
                    bases |= {b.attr for b in node.bases if isinstance(b, ast.Attribute)}
                    by_file[(str(path.relative_to(_ROOT)), node.name)] = bases

    settles = {"BaseSettings"}
    while True:
        grown = settles | {cls for (_, cls), bases in by_file.items() if bases & settles}
        if grown == settles:
            break
        settles = grown
    return {key for key, bases in by_file.items() if bases & settles}


def test_the_roster_names_every_settings_class_in_the_estate() -> None:
    """A new settings class must join `_SETTINGS`, not quietly go unchecked.

    This is the guard that lets the roster be explicit. Without it, "explicit" would just mean
    "whatever was true the day it was written" -- and the defect this file exists for reached nine
    classes precisely because nobody had a list.
    """
    rostered = {(f"{src}/src/{module.replace('.', '/')}.py", cls) for src, module, cls in _SETTINGS}
    discovered = _discovered_settings_classes()

    missing = sorted(discovered - rostered)
    stale = sorted(rostered - discovered)
    assert not missing, (
        f"{len(missing)} settings class(es) exist but are not in _SETTINGS, so nothing checks their env namespacing: {missing}. Add them to the roster."
    )
    assert not stale, f"_SETTINGS names {len(stale)} class(es) that no longer exist at that path: {stale}"
    assert len(discovered) >= 15, f"only {len(discovered)} settings classes discovered -- the AST walk found too few to be right"
