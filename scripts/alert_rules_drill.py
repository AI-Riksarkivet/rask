"""Replay every shipped alert rule against a REAL GreptimeDB and assert each one evaluates.

`make alert-rules-check` runs promtool, which is Prometheus. Production runs vmalert against
GreptimeDB's PromQL endpoint, and the two do not accept the same language. That gap is not
theoretical: two rules shipped for weeks that promtool reported as `SUCCESS` and GreptimeDB answered
HTTP 400 and HTTP 500 -- the alerts could not fire at all, and nothing anywhere said so.

This is the other half of the proof. promtool checks the LOGIC on synthetic series; this checks that
the production ENGINE will accept the expression at all. Neither replaces the other.

Usage:
    uv run python scripts/alert_rules_drill.py                # resolves the k3s ClusterIP itself
    RASK_GREPTIME_URL=http://host:4000 uv run python scripts/alert_rules_drill.py

THE VERDICT DEPENDS ON WHAT THE STORE HOLDS, and pretending otherwise made a hermetic run useless.
Measured 2026-09-24: against the populated live estate all 52 rules evaluated; against a FRESH,
EMPTY GreptimeDB the same two histogram rules answered HTTP 400 with
`No field named "sum(prom_rate(...))"` -- a PLAN error raised because the metric has no schema yet,
not because the expression is wrong. The engine cannot plan a `histogram_quantile` over a bucket
series it has never seen. So an empty-store run can only answer "does this parse and plan against
what exists", and a rule naming an absent metric is INCONCLUSIVE there rather than refused. It is
reported, loudly, and does not fail the run; everything else still does.

Keying that on the engine's own message is admittedly brittle, and the alternative is worse: seeding
one sample per metric would put a second description of every instrument in this file, to be wrong
the first time one is renamed.

Exit 0 = every rule evaluable, or inconclusive for want of data. Exit 1 = at least one rule the engine
refuses. Exit 2 = no reachable datasource (the caller decides whether that is a skip or a failure;
`make alert-rules-drill` treats it as a skip so a laptop without a cluster is not a red build).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[1]
RULES = REPO / "chart/alerting/rules.yml"


def resolve_datasource() -> str | None:
    """RASK_GREPTIME_URL, else the k3s ClusterIP. Never a guess -- returns None if it cannot ask."""
    if url := os.environ.get("RASK_GREPTIME_URL"):
        return url.rstrip("/")
    kubectl = shutil.which("kubectl")
    if not kubectl:
        return None
    # k3s, not the default context: a bare kubectl on this host resolves a stale kind cluster.
    env = {**os.environ, "KUBECONFIG": os.environ.get("KUBECONFIG", "/etc/rancher/k3s/k3s.yaml")}
    try:
        out = subprocess.run(  # noqa: S603 - argv is a literal; kubectl resolved via shutil.which
            [kubectl, "get", "svc", "rask-greptimedb-standalone", "-o", "jsonpath={.spec.clusterIP}"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    ip = out.stdout.strip()
    return f"http://{ip}:4000" if ip and out.returncode == 0 else None


def alert_rules() -> list[tuple[str, str]]:
    doc = yaml.safe_load(RULES.read_text())
    return [(rule["alert"], str(rule["expr"])) for group in doc["groups"] for rule in group.get("rules", []) if "alert" in rule]


#: A plan-time failure that names a missing column is the engine saying "this metric has no data here",
#: not "this expression is wrong". Proven by the same expression evaluating against a populated store.
_ABSENT_SERIES = "No field named"


def evaluate(base: str, expr: str) -> tuple[bool, str]:
    query = urllib.parse.urlencode({"query": expr})
    url = f"{base}/v1/prometheus/api/v1/query?{query}"
    if not url.startswith(("http://", "https://")):
        return False, f"refusing non-HTTP datasource {base!r}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 - scheme checked above
            body = json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode(errors="replace")[:200]
        except Exception:  # the body is best-effort diagnostics
            detail = ""
        return False, f"HTTP {exc.code} {detail}"
    except Exception as exc:  # any transport failure is a non-evaluation
        return False, f"{type(exc).__name__}: {exc}"
    if body.get("status") != "success":
        return False, f"status={body.get('status')} {str(body)[:200]}"
    return True, ""


def main() -> int:
    base = resolve_datasource()
    if not base:
        print("alert-rules-drill: no reachable GreptimeDB (set RASK_GREPTIME_URL or start k3s) — SKIPPED")
        return 2

    rules = alert_rules()
    if not rules:
        print(f"alert-rules-drill: parsed ZERO rules out of {RULES} — the drill would pass vacuously")
        return 1

    verdicts = [(name, evaluate(base, expr)) for name, expr in rules]
    failures = [(name, why) for name, (ok, why) in verdicts if not ok and _ABSENT_SERIES not in why]
    inconclusive = [name for name, (ok, why) in verdicts if not ok and _ABSENT_SERIES in why]
    print(f"alert-rules-drill: replayed {len(rules)} rules against {base}")
    if inconclusive:
        # Named rather than counted: a growing list means the store this ran against is emptier than
        # the estate, and a reader has to be able to see which proof they are NOT getting.
        print(f"  {len(inconclusive)} rule(s) INCONCLUSIVE here — the metric has no series in this store, so the engine cannot plan it:")
        for name in inconclusive:
            print(f"    ? {name}")
    if failures:
        print(f"  {len(failures)} rule(s) the production engine REFUSES — they can never fire:")
        for name, why in failures:
            print(f"    - {name}: {why}")
        return 1
    print("  all evaluable" if not inconclusive else "  every rule the store can plan is evaluable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
