# 0051. Cascade repair — detection, and the repair verb (2026-09-04)

A missed cascade hop was undetectable and unrepairable: the only remedy was re-publishing the
upstream table, which re-drives every consumer of it rather than the one edge that failed. Five
pieces closed it (C1, C3a, C3b, C3, C4, C2); what survives here is the reasoning that would otherwise
be re-derived.

**C3 and C4 cover DISJOINT failures and neither substitutes for the other.** A refusal counter can
only see a hop that ARRIVED and was declined — `_preflight` DROPs, and a DROP is an ACK, so Dapr does
not retry it; it routes to the declared dead-letter topic, where the halt is indistinguishable from an
exhausted delivery, and `medallion_stage_refused_total` is the only thing that says it was a decision
([[LH-151]]). It is
structurally blind to a hop that never happened. `medallion_cascade_lag` measures the other side: how
many source versions a destination has not consumed, which rises whether or not anything was refused.

**C3 NEEDED A THIRD SIGNAL, and the gap was found by measuring it rather than by reading it
(2026-09-11).** A lag is arithmetic over two reads, and the second is lineage's
`/datasets/{name}/producers` — gated router-level by `require_metadata_access`, which runs BEFORE
existence resolution. A destination that was never written therefore answers 403 exactly as a
forbidden one does, so the case C3 most exists for — a first-ever hop that never ran — produces no
series for `medallion_cascade_lag` to fire on. The detector's own `lag_for_edge` has a branch for it
(`if not consumed: lag = published`) that cannot be reached in production.

The discriminator is the SOURCE side, which is read from the catalog and answers for itself. Both
stores refusing usually means the project does not run that lane — on the live estate, 252 of 267 cells,
correctly silent. Usually, not always: an UNGOVERNED source refuses identically. Eight of
those 267 have one, and after excluding three whose source was dropped and three never written, TWO are
lanes that ARE running and read as lanes nobody runs. Nothing in this module can separate them, because
neither door offers an existence oracle by design; the repair is to govern the table, and the price of
leaving it is that the lane has no series at all. A cell whose source HAS published is a lane that is running, and the detector owes it
an answer; when it cannot give one the cell is BLIND, carried with its reason and published as
`medallion.cascade.lag_blind{reason}`, paged by `MedallionCascadeLagBlind` after 30m.

Two reasons, one closed vocabulary, because a field per state is precisely how two sibling readers came
to classify one identical refusal differently. `destination_invisible` — the source published into a
destination that cannot be read. `stores_disagree` — both stores answered and contradicted each other, a
consumed frontier ahead of the published version, which was counted without an identity and so could be
seen but never named. Measured 2026-09-11: of 15 cells with a published source, one of each.

Neither publishes a lag VALUE, deliberately. For `destination_invisible`, absent and forbidden are
indistinguishable at that door and the estate holds gold tables that exist with zero tuples, so a
guessed first-hop lag could be a confident number for a hop that had in fact run; for `stores_disagree`
there is no arithmetic to trust at all. The alert names the lane and the runbook branches on the reason.

*The lesson worth keeping is about the prose, not the metric.* `MedallionCascadeLag` described itself
as firing "for a hop that NEVER ARRIVED — which no counter can see". Read as a specification that was
a promise the rule could not keep, and it is what made the gap invisible for a week: the rule looked
like it already covered the case.

**The re-run verb: `POST /api/stage-runners/stages/rerun`.** Edge-addressed, so it re-drives ONE hop.

*The token is OPTIONAL.* It is the `table_published` event id, which the control outbox drops on ack
and no durable store retains, so a verb that required one could not be built. Supplied, the trigger is
verbatim and the stage runner's deterministic instance id reattaches at no extra call; absent, a fresh one
and a full recompute, which is the common case for the never-ran shape anyway.

*The rung is the EDGE's own* — `can_promote` on `namespace:<project>-gold` for silver→gold, exactly
what the stage runner asks when it runs the hop itself. `/produce`'s `can_administer` is coarser AND
different and would lock out the non-admin validator the rung exists for. Its sibling `terminate`
stays on `authorize_produce`: two verbs, two rungs, because stopping is not re-driving.

*No 409, and no forward.* The draft's liveness check needed a Ray job LISTING, and `GET /api/jobs/`
accepts no parameters at all — measured on this estate at 81,155 jobs / 164.7 MB in one response,
1179 MiB RSS against a 1536 MiB limit. The stage write is `mode="overwrite"`, overwrite-convergent, so
a racing fresh-token re-run reaches a correct final state and wastes only compute; the response says
so rather than implying a guarantee the listing could not make. Dropping the check dissolved the only
reason to forward to the stage runner, so the producer mints the trigger itself — which its own
`table_published` subscription already does, through the same `build_stage_trigger`.

**C3 shipped non-functional for weeks, and the chain is worth keeping.** Driven in-cluster for the
first time on 2026-09-04 it reported every edge failing on every tick. Seven layers, each hidden by
the one in front: the destinations env absent (stale deployment); `/api/v1/runs` where lineage serves
`/runs`; no credential at all; the shared token where the door binds a privileged subject to
`service-token-<identity>`; `can_get_metadata` missing because the root-warehouse `reader` grant does
not reach the medallion tiers' warehouse; lineage's subject ALLOWLIST, a different mechanism from the
grant; and finally the dedicated token that does not exist because `dedicatedServiceCredentials` is
false — which is the lakehouse register, row Q2-6 (drained 2026-09-10; in git history), not this work.

**Why six of those survived is the durable lesson.** A reader that cannot read reports `known=False`,
which publishes NOTHING and reports nothing wrong — so an empty series reads as a healthy cascade.
That is the same silent-loss shape the whole cascade-repair effort was about, committed inside the
detector written to catch it. A detector's failure path must be as loud as its finding.
