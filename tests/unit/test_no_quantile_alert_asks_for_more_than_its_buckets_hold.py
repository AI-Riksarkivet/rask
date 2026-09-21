"""A `histogram_quantile(...) > N` rule whose N exceeds the top bucket can never fire.

[[XC-067]], MEASURED LIVE 2026-09-21. `HttpServerLatencyHigh` asked for `p95 > 15000` against a
histogram whose largest finite bucket was 10,000 ms. `histogram_quantile` returns at most the upper
bound of the highest FINITE bucket when the quantile falls in `+Inf`, so the expression was capped at
10000 and the rule returned ZERO series — while `maintenance`, the service it exists to catch, sat at
a mean of 39,504 ms with 89% of its observations in the overflow bucket.

WHY A GENERIC GATE RATHER THAN A FIXED NUMBER. `scripts/alert_rules_drill.py` already replays every
rule against a real GreptimeDB and asserts each EVALUATES, and it passed this rule for weeks. That
check is sound and it is not this one: evaluating is not the same as being able to fire. The class of
defect is "a threshold above the measurable ceiling", and it recurs whenever either side moves — a
tightened bucket layout or a raised threshold — so it is checked as a relationship between the two
rather than as a remembered pair of numbers.

BOUND TO THE SOURCE OF TRUTH: the boundaries come from `service_kit.otel.HTTP_DURATION_BUCKETS_MS`,
which is what the services actually export. A copy of the numbers here would drift from the histogram
the moment somebody tuned it, and would then assert a ceiling that no longer exists.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from service_kit.otel import HTTP_DURATION_BUCKETS_MS


REPO = Path(__file__).resolve().parents[2]
RULES = REPO / "chart/alerting/rules.yml"

#: `histogram_quantile(...) ... > <number>` — the comparison this gate is about. The metric name is
#: captured so a rule over some OTHER histogram is not judged against the HTTP boundaries.
_QUANTILE_RULE = re.compile(r"histogram_quantile\s*\(.*?(\w*_bucket).*?\)\s*>\s*([0-9_.]+)", re.DOTALL)


def _rules() -> list[dict]:
    doc = yaml.safe_load(RULES.read_text())
    return [rule for group in (doc.get("groups") or []) for rule in (group.get("rules") or [])]


def test_the_rules_file_parses_and_has_quantile_rules() -> None:
    """Without this the gate passes by finding nothing to check."""
    found = [r for r in _rules() if "histogram_quantile" in str(r.get("expr", ""))]

    assert found, "no histogram_quantile rules parsed; this gate would check nothing"


def test_every_http_quantile_threshold_is_inside_the_bucket_range() -> None:
    """The defect, stated as a relationship: a threshold at or above the ceiling cannot be crossed."""
    top = max(HTTP_DURATION_BUCKETS_MS)
    unfireable: list[str] = []

    for rule in _rules():
        expr = str(rule.get("expr", ""))
        for metric, threshold in _QUANTILE_RULE.findall(expr):
            if not metric.startswith("http_server_duration"):
                continue  # judged against its own histogram's boundaries, not these
            value = float(threshold.replace("_", ""))
            if value >= top:
                unfireable.append(f"{rule.get('alert', '?')}: threshold {value:,.0f} >= top bucket {top:,.0f}")

    assert not unfireable, "these alerts can never fire — `histogram_quantile` is capped at the highest finite bucket:\n  " + "\n  ".join(unfireable)
