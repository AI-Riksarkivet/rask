"""`auth.audit` must silence the WHOLE trail, not the three services that arm it themselves.

`governed/audit.py` gates the `lance.audit` stream by the dedicated logger's LEVEL, so a service that
never calls `configure_audit` leaves it at NOTSET and inherits the root level `setup_logging` takes from
`RASK_LOG_LEVEL`. Measured on the live estate 2026-09-09: catalog, lineage and medallion set it and the
rest of the fleet inherited, so `auth.audit=false` silenced three services of ten while the others kept
emitting — and `RASK_LOG_LEVEL=WARNING`, the documented volume lever, deleted the trail of those others
with nothing reporting it (§ Q17-27).

`make_service_app` now applies `Settings.audit_enabled`, which is the app-side half. This is the CHART
half: one values key must render onto both spellings, or the lever is complete in code and partial in
the estate — which is the same defect wearing a different file.
"""

from __future__ import annotations

import yaml

from tests.unit.test_invariants import _helm_template


#: The two names this ONE decision travels under, and there are exactly two. Catalog, lineage and
#: medallion all declare the alias `LANCE_AUDIT_ENABLED` (the lance-ns inheritance); everything built by
#: `make_service_app` reads the shared `RASK_AUDIT_ENABLED`. `LINEAGE_READ_AUDIT_ENABLED` is deliberately
#: NOT here — it is a different control (`services.lineage.readAudit`, the per-read record), and folding
#: it in would make this gate demand that `auth.audit=false` disable a feature it does not govern.
_AUDIT_ENV = ("LANCE_AUDIT_ENABLED", "RASK_AUDIT_ENABLED")


def _audit_env(*values: str) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for doc in yaml.safe_load_all(_helm_template(*values)):
        if not doc or doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for env in container.get("env") or []:
                if env.get("name") in _AUDIT_ENV:
                    found.append((str(doc["metadata"]["name"]), str(env["name"]), str(env.get("value") or "")))
    return found


def test_the_lever_is_wired_at_all() -> None:
    rendered = _audit_env("medallion.enabled=true")
    assert rendered, "no Deployment is told anything about the audit trail — the values key reaches nothing"
    names = {name for name, _, _ in rendered}
    # Three services that emit and were told nothing before 2026-09-09: the factory-built fleet had no
    # shared flag at all, and lineage + medallion declared the catalog's alias while the chart rendered
    # it into the catalog alone. Named individually so a regression says WHICH half came loose.
    for expected in ("rask-ingest", "lineage", "medallion-producer"):
        assert any(expected in name for name in names), f"{expected} emits audit records and is told nothing; told: {sorted(names)}"


def test_setting_it_FALSE_silences_every_service_it_reaches() -> None:
    """The direction that matters: a deployment that says no must not keep a service emitting."""
    rendered = _audit_env("medallion.enabled=true", "auth.audit=false")
    still_on = [(svc, env) for svc, env, value in rendered if value != "false"]
    assert not still_on, f"auth.audit=false left these emitting, so the lever is partial: {still_on}"


def test_the_DEFAULT_keeps_the_trail_on() -> None:
    """The trail is evidence: forgetting to ask for it must not lose it."""
    rendered = _audit_env("medallion.enabled=true")
    off = [(svc, env) for svc, env, value in rendered if value != "true"]
    assert not off, f"the default silenced these, so an operator loses evidence by not deciding: {off}"
