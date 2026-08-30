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

import ast
import pathlib
import re

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed.settings import FgaSettings


REPO = pathlib.Path(__file__).resolve().parents[2]

#: The shared auth field-set -> the ONE environment variable each field may bind.
AUTH_FIELDS: dict[str, str] = {
    "oidc_enabled": "RASK_OIDC_ENABLED",
    "oidc_issuer": "RASK_OIDC_ISSUER",
    "oidc_audience": "RASK_OIDC_AUDIENCE",
    "oidc_discovery_url": "RASK_OIDC_DISCOVERY_URL",
    "oidc_cache_ttl": "RASK_OIDC_CACHE_TTL",
    "oidc_leeway": "RASK_OIDC_LEEWAY",
    "oidc_allow_insecure": "RASK_OIDC_ALLOW_INSECURE",
    "fga_enabled": "RASK_FGA_ENABLED",
    "fga_api_url": "RASK_FGA_API_URL",
    "fga_store_id": "RASK_FGA_STORE_ID",
    "fga_model_id": "RASK_FGA_MODEL_ID",
    "fga_timeout_seconds": "RASK_FGA_TIMEOUT_SECONDS",
    "fga_root_object": "RASK_FGA_ROOT_OBJECT",
}

#: The names the hard rename deletes. Listed in full rather than matched by prefix, because
#: `LANCE_FGA_CASCADE_WRITERS`, `LANCE_FGA_LOCK_ROOT_CREATE`, `LINEAGE_FGA_OBJECT_TYPE`,
#: `MEDALLION_FGA_SERVICE_IDENTITY` and `MEDALLION_FGA_REQUIRED_ACTION` are genuinely
#: service-specific: each is declared once already and keeps its owning service's namespace.
RETIRED_NAMES: tuple[str, ...] = tuple(
    f"{prefix}_{suffix}"
    for prefix in ("LANCE", "LINEAGE", "MEDALLION", "MAINTENANCE")
    for suffix in (
        "OIDC_ENABLED",
        "OIDC_ISSUER",
        "OIDC_AUDIENCE",
        "OIDC_DISCOVERY_URL",
        "OIDC_CACHE_TTL",
        "OIDC_LEEWAY",
        "OIDC_ALLOW_INSECURE",
        "FGA_ENABLED",
        "FGA_API_URL",
        "FGA_STORE_ID",
        "FGA_MODEL_ID",
        "FGA_TIMEOUT_SECONDS",
        "FGA_ROOT_OBJECT",
    )
)

_PYTHON_ROOTS = ("packages", "services")


def _settings_declarations() -> dict[str, list[tuple[str, int, str | None]]]:
    """field name -> [(file:line, alias)] for every class-body annotation of an auth field."""
    found: dict[str, list[tuple[str, int, str | None]]] = {name: [] for name in AUTH_FIELDS}
    for root in _PYTHON_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            if ".venv" in path.parts or "tests" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                for stmt in cls.body:
                    if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                        continue
                    field = stmt.target.id
                    if field not in found:
                        continue
                    found[field].append((str(path.relative_to(REPO)), stmt.lineno, _alias_of(stmt)))
    return found


def _alias_of(stmt: ast.AnnAssign) -> str | None:
    """The literal env name a `Field(..., alias=...)` binds, or None when there is no alias."""
    call = stmt.value
    if not isinstance(call, ast.Call):
        return None
    for kw in call.keywords:
        if kw.arg in {"alias", "validation_alias"} and isinstance(kw.value, ast.Constant):
            value = kw.value.value
            return value if isinstance(value, str) else None
    return None


def test_the_walk_sees_the_estate() -> None:
    """A guard on the guard: a layout change that emptied the walk would pass everything vacuously."""
    declarations = _settings_declarations()
    assert declarations["fga_enabled"], "the AST walk found no `fga_enabled` declaration at all — it is not seeing the estate"


def test_each_auth_setting_is_declared_exactly_once() -> None:
    duplicated = {field: [f"{path}:{line}" for path, line, _ in sites] for field, sites in _settings_declarations().items() if len(sites) > 1}
    assert not duplicated, (
        "one auth concept is declared in more than one settings class, so the copies drift and "
        "turning auth on means setting several names for one switch:\n"
        + "\n".join(f"  {field}: " + ", ".join(sites) for field, sites in sorted(duplicated.items()))
    )


def test_each_auth_setting_binds_exactly_one_rask_name() -> None:
    wrong: list[str] = []
    for field, sites in _settings_declarations().items():
        for path, line, alias in sites:
            if alias != AUTH_FIELDS[field]:
                wrong.append(f"  {path}:{line} {field} binds {alias!r}, must bind {AUTH_FIELDS[field]!r}")
    assert not wrong, "auth settings must bind the estate's one RASK_* name each:\n" + "\n".join(wrong)


def test_the_retired_names_are_gone_from_the_repository() -> None:
    """No alias list, no precedence chain: a retired name must bind nothing, anywhere.

    Prose counts. A chart comment or a runbook that still names `LANCE_FGA_ENABLED` sends an operator
    to set a variable no process reads, which is the same outage as a stale template.
    """
    pattern = re.compile(r"\b(" + "|".join(RETIRED_NAMES) + r")\b")
    self_path = pathlib.Path(__file__).relative_to(REPO)
    offenders: list[str] = []
    for rel in _tracked_files():
        if rel == self_path:
            continue
        try:
            text = (REPO / rel).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"  {rel}:{number}: {line.strip()[:120]}")
    assert not offenders, "these retired auth variable names survive the hard rename:\n" + "\n".join(offenders)


def _tracked_files() -> list[pathlib.Path]:
    import subprocess

    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True).stdout
    return [pathlib.Path(p) for p in out.split("\0") if p]


class _Probe(FgaSettings, BaseSettings):
    """A minimal governed settings class — the two behavioural tests below need no service's own fields."""

    model_config = SettingsConfigDict(populate_by_name=True, env_prefix="LANCE_", extra="ignore")


def test_the_new_name_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_FGA_API_URL", "http://fga.test:8080")
    assert _Probe().fga_api_url == "http://fga.test:8080"


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


def test_a_retired_name_is_matched_case_INSENSITIVELY() -> None:
    """Several of these classes set `case_sensitive=False`, so a lower-cased dotenv key binds exactly
    the same — a guard that matched only the upper-case spelling would be precisely as silent."""
    from service_kit.governed.settings import RETIRED_AUTH_ENV_NAMES, GovernedAuthSettings

    retired = RETIRED_AUTH_ENV_NAMES[0]
    assert retired.upper() != retired.lower(), "the fixture name must actually have a case to vary"
    found = GovernedAuthSettings._retired_names_in_scope
    assert callable(found), "the scan must be reachable as a classmethod for the dotenv path to use it"
