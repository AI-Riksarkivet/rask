# 0076. Code with no production caller is deleted with the tests that kept it green (owner, 2026-09-25)

The test audit found functions whose only callers were their own tests, so every one read as covered
while nothing in the estate ran it. Owner ruling: delete them with their tests, no compatibility shim.
Deleted: `executor.may_resubmit` and `executor.TERMINAL`, `task_registry.resolve_task`,
`lance_session.cpu_budget_cores`, `quality.assert_quality_on_batch` (with `test_quality_pre_commit.py`
and `test_dummy_quality_gate.py`, which tested nothing else), `medallion/services/s3_harvest.py`
(`S3PrefixSource`, `s3_input`), `tables._collect_tables`' `extra_roots` parameter, the `reason`
parameter of `dataplane.refuse_a_branch_this_door_cannot_honour`, and `repair.repair_drift_sync`.
`build_restamp_event` and `verify_stage_output` stay by the same ruling.

**`TaskRegistration.honours` stays, because the audit was wrong about it.** The transform declaration
door calls it (`catalog/api/v1/endpoints/transforms.py`), and that call is what refuses a transform
declared against a task that cannot honour its cardinality.

**`DURABLE_RECORD` now has no reader, and that supersedes one sentence above.** The 2026-09-04 entry
says an adapter advertising durability makes `may_resubmit` refuse to resubmit an `UNKNOWN` handle.
Nothing ever called it: the Ray watcher's resubmit machinery in `medallion/workflow.py` is
unconditional and sound only because both adapters withhold the capability. An engine that claims
`DURABLE_RECORD` therefore needs a resubmit branch that reads it, and the backlog rows that prescribed
`may_resubmit` now name the capability instead.

**Two deletions moved something that had to survive.** The LH-172 measurement lived only in the
deleted `cpu_budget_cores` test. Re-measured on pylance 12.0.0 by thread name, it does not survive:
the threads it counted are `lance_background`, which tracks the available CPUs (cgroup quota
included) and whose base neither variable sizes, while `LANCE_CPU_THREADS` does govern the `lance-cpu`
compute pool (LD36, LH-250). `lance_docs/PROVENANCE.md` records the per-pool counts. And the tests that drove `repair_drift_sync` and `extra_roots` now drive the
production `repair_drift` and `list_all_tables`. Measured before the change: with the dry run inverted
in `repair_drift`, all 414 maintenance tests passed; with the bound-seed merge removed from
`list_all_tables`, all 743 catalog tests passed and `tests/unit`'s result was unchanged.
