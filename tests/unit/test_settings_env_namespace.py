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

The rule this pins: **a settings field answers to no env var it does not declare.**

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

#: `BaseSettings` subclasses, as (src root, module, class). Explicit rather than import-walked: a
#: settings class that will not import without its service's deps would turn a discovery walk into a
#: skip, and a skipping gate measures nothing.
_SETTINGS: list[tuple[str, str, str]] = [
    ("packages/service-kit", "service_kit.config", "Settings"),
    # `MediaSettings`, renamed from a second class called `Settings` (SK-10) — one distribution held
    # two of that name, on `RASK_*` and `MEDIA_*` respectively. `service_kit.media.config.Settings`
    # survives as an alias for the three services that import it, so this roster names the DEFINITION.
    ("packages/service-kit", "service_kit.media.config", "MediaSettings"),
    ("packages/lineage-kit", "lineage_kit.config", "LineageSettings"),
    ("services/lineage", "lineage.core.config", "LineageSettings"),
    ("services/medallion", "medallion.core.config", "MedallionSettings"),
    ("services/notifications", "notifications.api.settings", "IngressSettings"),
    ("services/catalog", "catalog.core.config", "Settings"),
    ("services/maintenance", "maintenance.core.config", "MaintenanceSettings"),
    # These two reach `populate_by_name` by declaring it next to a GovernedAuthSettings mixin, which
    # is why grepping for the flag on a `class X(BaseSettings)` line misses them.
    ("services/notifications", "notifications.config", "NotificationsSettings"),
    ("services/flows", "flows.config", "FlowsSettings"),
    # The ingest service's OPERATIONAL settings: every `RASK_INGEST_*` / upstream-address knob.
    ("services/ingest", "ingest.config", "IngestSettings"),
    # The FRONT DOOR, which had no settings class at all until FLEET-ENV-SCATTER: sixteen raw
    # `os.environ.get` reads, several of them per request. Neither `populate_by_name` nor
    # `env_prefix` — every field carries the deployed variable's full name as an explicit alias.
    ("services/gateway", "gateway.config", "GatewaySettings"),
    # Subclasses of service_kit.media.config.Settings. None declares `populate_by_name`, and
    # GovernedAuthSettings is a plain mixin rather than a BaseSettings, so none inherited it.
    ("services/search", "search.core.config", "SearchSettings"),
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
