"""Every lineage outcome that loses provenance is read by an alert rule, and none is emitted by nothing.

THE SAME SELF-CONCEALING SHAPE `test_a_counted_halt_is_an_audible_halt` guards for the medallion, on the
service where the loss is now quietest. Lineage acks two classes it can never repair — a payload that
does not parse, and a run carrying no author — because a DROP on a subscription with a `deadLetterTopic`
parks a duplicate on every roll (measured 2026-09-18: 44 refusals, 44 parks, 37 of them ONE unauthored
run id re-presented at pod start). Acking is right, and it removes the only thing that used to make
those events visible: they no longer reach `/lineage-dlq`, so `LineageIngestDeadLettering` cannot see
them. All that is left is the count, which makes the count the safety argument for acking at all rather
than a statistic.

THE OTHER DIRECTION MATTERS EQUALLY. An enum value nothing emits is a label an operator can wait on
forever — the same never-fires that
`test_invariants::test_every_first_party_ALERT_names_a_metric_the_service_actually_EMITS` guards from
the rule side. `DROPPED` became exactly that when the malformed arm moved to `UNREPAIRABLE`.

The exemptions are the honest part: an outcome that is throughput or visibility rather than loss is
named here with its reason, so adding a losing outcome without an alert fails rather than passing
quietly.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from lineage.core.metrics import Outcome


REPO = Path(__file__).resolve().parents[2]
_RULES = REPO / "chart/alerting/rules.yml"
_CONSUMER = REPO / "services/lineage/src/lineage/services/consumer.py"
_DLQ = REPO / "services/lineage/src/lineage/api/dapr.py"
#: The HTTP ingest door — the THIRD emission site, and the only one a sidecar-less producer reaches
#: (the Ray lane, every runner, any external OpenLineage producer). Reading only the two Dapr sites
#: left this gate blind to whether that door classified its refusals at all; it did not ([[LH-184]]).
_HTTP_INGEST = REPO / "services/lineage/src/lineage/api/v1/endpoints/ingest.py"

#: Outcomes that are NOT a loss, each with the reason it needs no rule of its own.
_NOT_A_LOSS: dict[Outcome, str] = {
    Outcome.INGESTED: "the success path — throughput, and its absence is covered by the staged/relay rules",
    Outcome.RETRIED: "transient, and the sidecar redelivers; a retry that never succeeds ends as dead_lettered",
    Outcome.PARKED_ALREADY_RECORDED: "visibility, not loss — the graph already holds the run (LH-148)",
    # The REPAIR path, and the one outcome here that is the inverse of a loss: the parking route
    # re-presented the delivery and the graph now holds the run, confirmed by a second read rather than
    # by an ack status (LH-148). It needs no rule of its own because the failure it could mask already
    # has one: a park that does NOT recover stays `dead_lettered`, which `LineageDeadLettering` reads.
    # A rising `recovered` rate means the BUS path is failing and the parking lane is catching it — a
    # real signal, but a health one rather than a loss one, and the bus's own failure surfaces on
    # `refused`/`retried` before it reaches here.
    Outcome.RECOVERED: "the repair path — the graph holds the run; an unrecovered park is still dead_lettered (LH-148)",
}


def _alerted_outcomes() -> set[str]:
    """Every `lance_lineage_outcome` label value any alert expression selects on."""
    rules = yaml.safe_load(_RULES.read_text())
    exprs = " ".join(rule.get("expr", "") for group in rules["groups"] for rule in group.get("rules", []))
    return set(re.findall(r'lance_lineage_outcome\s*=\s*"([a-z_]+)"', exprs))


def _emitted_outcomes() -> set[str]:
    """Every outcome the service actually records, read from the two call sites.

    Matched on ANY `Outcome.NAME` in those files rather than on the `record_outcome(...)` call shape:
    `api/dapr.py` picks its value with a ternary INSIDE the call, so a pattern anchored on the call
    would miss the else-branch and report a live outcome as dead — a gate wrong in the direction that
    causes work rather than the one that hides a hole, but wrong.
    """
    source = _CONSUMER.read_text() + _DLQ.read_text() + _HTTP_INGEST.read_text()
    return {Outcome[name].value for name in re.findall(r"\bOutcome\.([A-Z_]+)\b", source) if name in Outcome.__members__}


def test_every_LOSING_outcome_is_read_by_an_alert() -> None:
    """A loss nobody is told about is the failure acking was supposed to trade away, not accept."""
    losing = {o.value for o in Outcome if o not in _NOT_A_LOSS}

    assert losing <= _alerted_outcomes(), f"these lineage outcomes lose provenance and no alert reads them: {sorted(losing - _alerted_outcomes())}"


def test_every_outcome_is_actually_EMITTED() -> None:
    """An enum value nothing records is a label an operator can wait on forever."""
    declared = {o.value for o in Outcome}

    assert declared <= _emitted_outcomes(), f"these outcomes are declared and emitted by nothing: {sorted(declared - _emitted_outcomes())}"


def test_the_exemptions_name_outcomes_that_exist() -> None:
    """A stale exemption silently re-opens the hole it was written to justify."""
    assert set(_NOT_A_LOSS) <= set(Outcome)
