"""One auth concept, one declaration, one environment-variable name.

THE DEFECT THIS GATE REFUSES. The OIDC + OpenFGA field-set was declared FIVE times under FOUR
prefixes: the shared `GovernedAuthSettings` mixin and the catalog's inline twin under `LANCE_*`,
lineage's under `LINEAGE_*`, maintenance's under `MAINTENANCE_*`, medallion's under `MEDALLION_*`.
That is not a naming inconvenience, it is a correctness hole with a measured shape:

* Turning authorization on estate-wide meant setting FOUR different names for one switch, and the
  chart carried a prefix-PARAMETERISED helper (`lance.governedOidcEnv`) whose only reason to exist
  was that the estate could not agree on a name.
* The copies drifted, silently and in both directions. `medallion` declared `fga_store_id: str = ""`
  where every other copy declared `str | None = None`, and dropped the `ge=0.1` floor on
  `fga_timeout_seconds`; `lineage` and `medallion` never grew the HTTPS-issuer validator that the
  mixin and the catalog both enforce, so an `http://` issuer boots there and answers 401 on every
  VALID bearer — indistinguishable, to the caller, from an expired token.

A copy cannot drift if there is no copy, so the invariant is structural: each auth field is declared
in exactly ONE class in the whole Python estate, and binds exactly ONE `RASK_*` variable. The old
prefixed names are DELETED, not aliased — a deployment that still sets `LANCE_OIDC_ENABLED` must get
an unauthenticated service loudly, by way of a name that binds nothing, rather than half an estate
that authenticates and half that does not.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed.settings import FgaSettings


class _Probe(FgaSettings, BaseSettings):
    """A minimal governed settings class — the two behavioural tests below need no service's own fields."""

    model_config = SettingsConfigDict(populate_by_name=True, env_prefix="LANCE_", extra="ignore")


def test_a_retired_name_in_the_environment_is_a_startup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The textual gate above cannot see a CLUSTER that still sets the old name.

    `populate_by_name` plus a service `env_prefix` teaches pydantic-settings the bare field name as a
    second lookup, so `LANCE_FGA_ENABLED` would still have bound on every class prefixed `LANCE_` and
    on nothing else — the pre-rename estate, disagreeing pod by pod, under new names.
    """
    monkeypatch.setenv("LANCE_FGA_ENABLED", "true")
    with pytest.raises(ValidationError, match="RASK_FGA_"):
        _Probe()


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#
# "DELETED" HAS TO MEAN DELETED ON EVERY SOURCE THE CLASS READS, not just the process environment.
# The refusal guard originally scanned `os.environ` alone, justified by "every deployment path in this
# estate sets real env vars" — but FOUR settings classes declare `env_file=".env"` (`ingest.auth`,
# `controlplane`, `gateway`, `flows`), and `ingest.auth` pairs it with `env_prefix="LANCE_"`. So a
# retired name in a dotenv BOUND, silently, while the guard that exists to stop exactly that stayed
# quiet — and three shipped docstrings claimed "a retired name must bind nothing, anywhere".


def test_a_retired_name_in_a_DOTENV_is_refused_like_one_in_the_environment(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hole the first version of the guard left open.

    A hard rename whose enforcement depends on WHICH FILE the operator happened to put the old name in
    is not a hard rename — it is the silent half-authenticated estate the rename was chosen to prevent,
    reachable by a slightly different deployment habit.
    """
    from service_kit.governed.settings import RETIRED_AUTH_ENV_NAMES, GovernedAuthSettings

    retired = RETIRED_AUTH_ENV_NAMES[0]
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{retired}=true\n", encoding="utf-8")

    class _Probe(GovernedAuthSettings, BaseSettings):
        model_config = SettingsConfigDict(env_file=str(dotenv), extra="ignore", case_sensitive=False, populate_by_name=True)

    # Nothing in the PROCESS environment — the whole point is that the dotenv is the only source.
    for name in RETIRED_AUTH_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError) as excinfo:
        _Probe()
    assert retired in str(excinfo.value), f"a retired name in a dotenv bound silently: {excinfo.value}"
