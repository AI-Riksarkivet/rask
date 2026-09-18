"""`lance.audit` gets its OWN table and its OWN retention, not the telemetry database's.

[[XC-003]]. Measured on the live store 2026-09-18: **8,590,872 audit rows** sit inside
`opentelemetry_logs` alongside **114,938,016** telemetry rows, and that table carries
`ttl = '14days'` — so the estate's governance trail is destroyed on the same two-week clock as a
debug log, and `SELECT * FROM opentelemetry_logs WHERE body = 'audit'` is the only thing separating
them. A trail that ages out with the noise it rides in is not a trail.

BOTH MECHANISMS WERE VERIFIED AGAINST THE RUNNING SERVER (GreptimeDB v1.1.1) rather than taken from
documentation:

  * `x-greptime-log-table-name: <name>` on `POST /v1/otlp/v1/logs` creates and writes that table — a
    protobuf record carrying the header produced `xc003_probe_tbl`, which is how the split is done
    without a second exporter endpoint.
  * `ALTER TABLE <t> SET 'ttl'='365days'` is read back by `SHOW CREATE TABLE` as
    `ttl = '11months 30days 3h 50m 24s'`, so a per-table retention genuinely overrides the
    database's.

WHAT THIS DOES NOT FIX, said here because a green gate reads as a solved problem: `:4000/v1/sql`
takes no credential. Measured the same day from inside the cluster with no auth at all — CREATE 200,
INSERT 200, `DELETE ... WHERE v = 'probe'` 200 `affectedrows: 1`, DROP 200. Splitting the table
changes what ages out, not who may erase it; the credential is the rest of XC-003.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from chart_yaml import FAST_LOADER


REPO = Path(__file__).resolve().parents[2]


def _render(**sets: str) -> list[dict]:
    if not shutil.which("helm"):  # pragma: no cover - CI installs helm
        pytest.skip("helm not on PATH")
    cmd = ["helm", "template", "rask", str(REPO / "chart")]
    cmd += ["--set-string", "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum"]
    cmd += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    cmd += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    cmd += ["--set", "image.localImages=true", "--set", "observability.enabled=true"]
    for key, value in sets.items():
        cmd += ["--set", f"{key.replace('__', '.')}={value}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.load_all(out, Loader=FAST_LOADER) if d]


def _collector_config(docs: list[dict]) -> dict:
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and "otel" in doc.get("metadata", {}).get("name", ""):
            for value in doc.get("data", {}).values():
                # `load_all` rather than `load`, matching the other chart gates: FAST_LOADER is a SAFE
                # loader (see `chart_yaml`), and this spelling says so without a suppression.
                for parsed in yaml.load_all(value, Loader=FAST_LOADER):
                    if isinstance(parsed, dict) and "exporters" in parsed:
                        return parsed
    raise AssertionError("no OTel Collector config rendered — is observability.enabled on?")


def _ttl_job_script(docs: list[dict]) -> str:
    for doc in docs:
        if doc.get("kind") == "Job" and "greptimedb-ttl" in doc.get("metadata", {}).get("name", ""):
            return "\n".join(doc["spec"]["template"]["spec"]["containers"][0]["args"])
    raise AssertionError("no greptimedb-ttl Job rendered")


def test_the_collector_writes_audit_to_its_own_table() -> None:
    config = _collector_config(_render())
    audit = [name for name in config["exporters"] if "audit" in name]

    assert audit, f"no audit exporter — every log still lands in one table: {sorted(config['exporters'])}"
    headers = config["exporters"][audit[0]]["headers"]
    assert headers.get("x-greptime-log-table-name"), f"the audit exporter names no table, so it writes opentelemetry_logs like everything else: {headers}"


def test_the_audit_pipeline_is_wired_not_merely_declared() -> None:
    """An exporter no pipeline references renders fine and moves nothing."""
    config = _collector_config(_render())
    audit = [name for name in config["exporters"] if "audit" in name]
    referenced = {exporter for pipeline in config["service"]["pipelines"].values() for exporter in pipeline["exporters"]}

    assert audit, "no audit exporter at all, so this assertion would hold over an empty set"
    assert set(audit) <= referenced, f"the audit exporter is declared but no pipeline sends to it: {audit} vs {sorted(referenced)}"


def test_audit_rows_do_not_ALSO_go_to_the_shared_table() -> None:
    """A split that copies rather than routes doubles the store and leaves the 14d copy authoritative."""
    config = _collector_config(_render())
    ordinary = [name for name, pipeline in config["service"]["pipelines"].items() if name.startswith("logs") and "audit" not in name]

    assert ordinary, "no ordinary logs pipeline left — telemetry logs stopped being collected"
    for name in ordinary:
        exprs = str(config["processors"])
        assert "audit" in exprs, f"pipeline {name} has no filter excluding audit records, so every audit row is written twice"


def test_the_audit_table_gets_its_own_retention() -> None:
    """Without this the split is cosmetic: a new table inherits the database's 14d."""
    script = _ttl_job_script(_render())

    assert "ALTER TABLE" in script, "the retention hook sets only a database TTL, so the audit table ages out with the telemetry"


def test_the_audit_retention_is_longer_than_the_telemetry_retention() -> None:
    """The whole point. Equal retentions would be a table split that changes nothing."""
    docs = _render()
    script = _ttl_job_script(docs)
    values = next(iter(yaml.load_all((REPO / "chart" / "values.yaml").read_text(), Loader=FAST_LOADER)))
    audit_ttl = values["observability"].get("auditRetention", "")
    telemetry_ttl = values["observability"]["retention"]

    assert audit_ttl, "observability.auditRetention is unset, so the audit table has no retention of its own"
    assert audit_ttl != telemetry_ttl, f"audit retention {audit_ttl!r} equals telemetry retention {telemetry_ttl!r} — the split buys nothing"
    assert audit_ttl in script, f"the hook does not apply {audit_ttl!r}: {script}"
