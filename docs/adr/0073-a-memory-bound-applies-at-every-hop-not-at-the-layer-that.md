# 0073. A memory bound applies at every hop, not at the layer that happens to set it (2026-09-22)

**What was decided before.** `compact_one` was deliberately a faithful pass-through for its three
compaction bounds: the safe default lived in `MaintenanceSettings` and was applied by the SWEEP, so a
caller with its own memory story could reach Lance's native ceilings on purpose. A test pinned it by
name — `test_no_batch_size_leaves_lance_defaulting` — and its docstring argued the layering
explicitly: "nothing in the deployed estate reaches Lance's unbounded default any more".

**Why it is reversed.** The claim was true only while every hop agrees. `DatasetWorkItem`'s three
bound fields are `int | None` because the wire model must be able to say "the policy said nothing",
and that `None` crosses the queue for every unpolicied dataset — which is most of the estate ([[LH-191]]:
27 policy records against ~570 datasets). So the unbounded state was reachable by a planner/worker
version skew or a hand-published unit, not only by a hypothetical caller. Lance's defaults there are
an 8192-ROW read batch and the HOST's core count, which is incident #93 exactly. The set the
pass-through was serving is empty: there is one production caller and it always sets the bounds.

**The adjacent parameter had already gone the other way.** `rewrite_slots` on the SAME function
defaults to 1 with the rationale written beside it — "a caller that does not care is bounded rather
than unbounded". Two parameters on one signature disagreeing about whether a careless caller is safe
is the drift, not the intent.

**The rule.** A bound whose absence can OOM a pod is applied where it is CONSUMED, not only where it
is configured, and `None` from an optional wire field means the floor — never no ceiling. The numbers
live once (`maintenance.core.config`, `catalog.services.maintenance.COMPACTION_BOUND`) and are read by
both the settings defaults and the parameter defaults, because two spellings of 64 is how a bound gets
raised in one place and kept in the other. What survives of the old decision is the other direction:
a caller that names a value still gets exactly that value.

**Gated by** `tests/unit/test_a_compaction_door_is_bounded.py`, which resolves the keywords every
`compact_files()` call site in the four lakehouse services actually carries — following one hop of
indirection, so a locally-built dict, a constant-updated one and a parameter handed in by a caller in
the same module all resolve — and requires all three bounds at each.
