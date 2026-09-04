"""Every counter that records a CASCADE HALT is read by an alert rule.

`tests/unit/test_invariants.py::test_every_first_party_ALERT_names_a_metric_the_service_actually_EMITS`
already guards one direction — a rule naming a series nobody writes never fires, and never-fires is
what a working alert looks like. This is the OTHER direction, and it is self-concealing in the same
way: a counter that is faithfully emitted and read by no rule is a halt nobody hears.

The medallion needs it more than any service, because of how its refusals END. A `_preflight` refusal
returns DROP, and a DROP is an ACK — Dapr neither redelivers nor dead-letters, so there is no DLQ
alarm, no retry, no error, and the cascade simply stops for that tenant. `record_refused`'s own
docstring says the counter IS the instrument: *"a tenant trigger arriving with registry resolution off
is a deployment gap that halts that tenant's cascade permanently — both are worth an alert, and
neither raises one from a log line."* It was worth an alert and had none.

The allow-list is the honest part. A counter may legitimately be observation-only — throughput, bytes,
a verdict breakdown already covered by a sibling rule — and this gate is worthless if it forces a rule
per counter. So every exemption is NAMED with its reason, and adding a halt counter without either an
alert or an entry here fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[2]
_METRICS = REPO / "services/medallion/src/medallion/core/metrics.py"
_RULES = REPO / "chart/alerting/rules.yml"

#: Counters that record a HALT — work the cascade will not do, that nothing else reports.
_HALT_COUNTERS = {
    "medallion.stage.refused",
    "medallion.stage.denied",
    "medallion.dlq.parked",
}

#: Counted on purpose, alerted by something else or by nothing, each with its reason.
_NOT_ALERTED: dict[str, str] = {
    "medallion.stage.bytes": "throughput, not a halt",
    "medallion.stage.rows": "throughput, not a halt",
    "medallion.stage.transitions": "throughput, not a halt",
    "medallion.stage.outcome": "the non-succeeded verdicts are alerted by MedallionStageOutcomesFailing",
    "medallion.stage.quality_blocked": "a deliberate gate decision, surfaced on the Perses board; blocking is the feature",
    "medallion.stage.media_underivable": "a per-row data property, not a cascade halt",
    "medallion.promotion.outcome": "a human decision lane; a held promotion is answered on /promotions",
    "medallion.train.outcome": "the training lane, outside the cascade this file guards",
    # ROUTINE, not a halt: several movers subscribe to one tier's topic, so a trigger for a lane this
    # mover does not own is expected traffic and `> 0` would alert constantly. The condition its
    # docstring actually cares about — a lane with NO consumer — is `other_lane` rising while
    # `transitions` stays flat, a ratio rule, not a threshold. Worth building; not built, so not
    # claimed here.
    "medallion.stage.other_lane": "routine in a multi-lane estate; the no-consumer case needs a ratio rule, not a threshold",
}


def _emitted() -> set[str]:
    names = set(re.findall(r'create_counter\(\s*"([a-z_.]+)"', _METRICS.read_text()))
    assert names, "no counters parsed from the medallion metrics module — this gate would pass vacuously"
    return names


def _promql() -> str:
    return yaml.safe_dump(yaml.safe_load(_RULES.read_text()))


def _series(counter: str) -> str:
    """OTLP -> Prometheus: dots become underscores, a counter gains `_total`."""
    return counter.replace(".", "_") + "_total"


def test_the_walk_sees_the_counters() -> None:
    assert _emitted() >= _HALT_COUNTERS, f"a halt counter named here no longer exists: {sorted(_HALT_COUNTERS - _emitted())}"


@pytest.mark.parametrize("counter", sorted(_HALT_COUNTERS))
def test_every_HALT_counter_is_read_by_an_alert(counter: str) -> None:
    assert _series(counter) in _promql(), (
        f"{counter} records work the cascade will NOT do and no alert rule reads it. A DROP is an ack, "
        "so there is no DLQ, no retry and no error — this counter is the only evidence the halt "
        f"happened. Add a rule over {_series(counter)}, or move it to _NOT_ALERTED with the reason."
    )


def test_every_counter_is_classified() -> None:
    """No counter may be neither a halt nor an explained exemption — that is how the next one is
    added and quietly read by nothing."""
    unclassified = sorted(_emitted() - _HALT_COUNTERS - set(_NOT_ALERTED))
    assert not unclassified, (
        f"these medallion counters are neither declared halts nor exempted: {unclassified}. "
        "Add to _HALT_COUNTERS (and give it a rule) or to _NOT_ALERTED with the reason it needs none."
    )


def test_the_exemptions_still_exist() -> None:
    stale = sorted(set(_NOT_ALERTED) - _emitted())
    assert not stale, f"exempted counters that no longer exist: {stale}"
