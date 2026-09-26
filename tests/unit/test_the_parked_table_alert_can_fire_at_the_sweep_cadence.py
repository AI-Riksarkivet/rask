"""`MaintenanceTableParked` can fire only while the sweep runs often enough for its window.

The rule is `sum by (table_id) (increase(compaction_tables_parked_total[W])) > 0` held for 24h. An OTel
counter exports nothing before its first add, so each worker's per-table series first appears at 1 and
`increase()` never sees that count. A deploy replaces every worker, so up to `replicas` ticks in a row
can each be a new worker's first count, and a late unit adds up to one `ackWait`. The window must hold

    (replicas + 1) x sweep period + ackWait

or the clock restarts on every deploy and the alert never fires. Measured with promtool 2026-09-26: two
workers ticking hourly and replaced together at 12h fired at 26h with W = 4h and never with 2h or 3h
(`chart/alerting/rules_test.yml` carries the 4h case). This reads every number from the rendered chart,
so a slower `maintenance.schedule`, more workers or a longer `ackWait` fail here rather than in silence.
"""

from __future__ import annotations

import pathlib
import re
from itertools import product

import pytest
import yaml

from tests.unit.chart_render import DEFAULT_ARGS, render


REPO = pathlib.Path(__file__).resolve().parents[2]
RULE = "MaintenanceTableParked"
#: The fail-closed prod guards' dummy inputs, the set `scripts/prod_render_check.sh` uses.
PROD_ARGS: tuple[str, ...] = (
    "-f", str(REPO / "chart/values-prod.yaml"),
    "--set", "image.catalog.tag=v0",
    "--set", "frontend.image.tag=v0",
    "--set", "dapr.appToken=ci-dummy-token-0000000000",
    "--set", "age.password=ci-dummy-pw",
    "--set", "minio.secretKey=ci-dummy-key",
    "--set", "backups.volumeSnapshot.snapshotClassName=csi-snapclass",
    "--set", "ingress.host=lance.example.com",
    "--set", "image.repository=ghcr.io/example/rask",
)  # fmt: skip
_UNITS = {"h": 3600, "m": 60, "s": 1}
_DESCRIPTORS = {"@hourly": 3600, "@daily": 86400, "@midnight": 86400}


def _seconds(duration: str) -> int:
    """A Go duration as `@every` and `ackWait` spell it (`1h30m`, `120s`)."""
    parts = re.findall(r"(\d+)([hms])", duration)
    if not parts or "".join(n + u for n, u in parts) != duration:
        raise ValueError(f"not a duration this gate reads: {duration!r}")
    return sum(int(n) * _UNITS[u] for n, u in parts)


def _field(spec: str, low: int, high: int) -> set[int]:
    """One cron field: `*`, `N`, `a-b`, `*/n`, `a-b/n`, and comma lists of those."""
    values: set[int] = set()
    for part in spec.split(","):
        base, _, step = part.partition("/")
        start, end = (low, high) if base == "*" else (int(base.split("-")[0]), int(base.split("-")[-1]))
        values.update(range(start, end + 1, int(step or 1)))
    return values


def sweep_period(schedule: str) -> int:
    """The LONGEST gap, in seconds, between two firings of a Dapr cron `schedule`.

    Dapr's cron binding takes `@every <duration>`, a descriptor, or a cron line whose first field is the
    second. Day, month and weekday fields must be `*`: a schedule that skips days has a gap this does not
    compute, so it is refused rather than guessed.
    """
    schedule = schedule.strip()
    if schedule.startswith("@every "):
        return _seconds(schedule.removeprefix("@every ").strip())
    if schedule in _DESCRIPTORS:
        return _DESCRIPTORS[schedule]
    fields = schedule.split()
    if len(fields) == 5:
        fields = ["0", *fields]
    if len(fields) != 6 or any(f != "*" for f in fields[3:]):
        raise ValueError(f"not a schedule this gate reads: {schedule!r}")
    seconds, minutes, hours = _field(fields[0], 0, 59), _field(fields[1], 0, 59), _field(fields[2], 0, 23)
    firings = sorted(h * 3600 + m * 60 + s for h, m, s in product(hours, minutes, seconds))
    return max(later - earlier for earlier, later in zip(firings, [*firings[1:], firings[0] + 86400], strict=True))


def _window() -> int:
    rules = yaml.safe_load((REPO / "chart/alerting/rules.yml").read_text())
    expr = next(rule["expr"] for group in rules["groups"] for rule in group["rules"] if rule.get("alert") == RULE)
    found = re.search(r"compaction_tables_parked_total\[(\w+)\]", expr)
    assert found, f"{RULE} no longer reads a window off compaction_tables_parked_total: {expr}"
    return _seconds(found.group(1))


def _meta(component: dict) -> dict[str, str]:
    return {entry["name"]: str(entry.get("value", "")) for entry in component["spec"]["metadata"]}


@pytest.mark.parametrize(
    ("schedule", "period"),
    [("@every 120s", 120), ("@every 1h15m", 4500), ("0 0 * * * *", 3600), ("0 */30 * * * *", 1800), ("0 0 */2 * * *", 7200), ("0 0,45 * * * *", 2700)],
)
def test_the_period_is_the_longest_gap_between_firings(schedule: str, period: int) -> None:
    assert sweep_period(schedule) == period


@pytest.mark.parametrize("schedule", ["0 0 0 * * 1", "@weekly", "@every 2d"])
def test_a_schedule_this_gate_cannot_read_is_refused(schedule: str) -> None:
    with pytest.raises(ValueError, match="this gate reads"):
        sweep_period(schedule)


@pytest.mark.parametrize("overlay", [pytest.param(DEFAULT_ARGS, id="default"), pytest.param(PROD_ARGS, id="prod")])
def test_the_window_holds_a_deploy_at_the_sweep_cadence(overlay: tuple[str, ...]) -> None:
    docs = render(*overlay)
    components = [d for d in docs if d.get("kind") == "Component"]
    cron = next(c for c in components if c["spec"]["type"] == "bindings.cron" and c["metadata"]["name"].endswith("maintenance-cron"))
    work = next(c for c in components if _meta(c).get("name") == "lance-dapr-maintenance-work")
    workers = next(d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("maintenance-worker"))
    period, ack_wait, replicas = sweep_period(_meta(cron)["schedule"]), _seconds(_meta(work)["ackWait"]), int(workers["spec"]["replicas"])

    needed = (replicas + 1) * period + ack_wait

    assert needed < _window(), (
        f"{RULE}'s window is {_window()}s, but {replicas} workers sweeping every {period}s with ackWait {ack_wait}s "
        f"can leave {needed}s between counts it sees, so a deploy restarts its clock and it never fires"
    )
