# 0039. Lineage records what happened to DATA; an authorization denial is not a data event (2026-08-16)

**Ruled** while closing the notifications coverage register, which proposed emitting an OpenLineage
FAIL when a medallion stage runner is denied by FGA (`transform.py`'s `medallion_stage_denied` path). It
should not, and the existing behaviour — drop, count, log — is right.

**An OpenLineage `RunEvent` describes a RUN of a JOB over DATASETS.** `FAIL` asserts that a dataset's
production was attempted and failed. When the stage runner is denied, nothing is read and nothing is
written: no data is touched. A FAIL there mints provenance for a non-event, and a graph that records
runs which never ran answers every later question wrongly — *"where did version 7 come from"* and
*"what has touched this table"* both degrade, which is the one thing provenance exists to answer.
Gaps in provenance are recoverable; fiction in it is not.

**A denial is also a STEADY STATE, not an incident.** A stage runner that is permanently un-granted would
emit a FAIL on every trigger, forever, turning the provenance graph into an alert stream. The
observability rule is the ordinary one: a repeating operational condition is a METRIC, not an event —
bounded cardinality, alertable, cheap. The estate already does exactly this
(`record_denied` -> `_stage_denied`, labelled by transition, plus a `medallion_stage_denied` warning),
and `test_stage_runner_denied_when_not_authorized` pins the silence deliberately.

**The user-facing gap the proposal was really about is REAL, and belongs on the CONTROL lane.** The
person who started a cascade should learn that it stopped. But "your run was blocked because a stage runner
lacks a grant" is a GOVERNANCE fact about a principal, not a fact about data — the same distinction
that keeps grants off the lineage lane today. It names a person, so it is a `NAMED_ACTIONS` control
event, and `lance.originator` (added 2026-08-16) is the identity that makes it addressable at all.

**The line, stated once:** lineage answers *what happened to this dataset and who produced it*;
the control lane answers *what changed for this person*; metrics answer *how often is this
happening*. A fact that names a principal and touches no data is never lineage.
