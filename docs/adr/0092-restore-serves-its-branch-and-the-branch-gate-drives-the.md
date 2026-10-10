# 0092. restore serves its branch, and the branch gate drives the doors (LH-272, 2026-10-05)

`POST /v1/table/{id}/restore` serves `branch`: the upstream `dir` `restore_table` restores the named ref (measured on
pylance 12.0.0: a branch at v3 restored to v2 went to v4, main kept its version), so the door opens the ref first, which
answers 22 `TableBranchNotFound` for a missing branch and 11 for a version the branch lacks where upstream answered 4
with a storage path, and the RESTORE lineage edge is read back off that branch rather than main. `create_index` and
`create_scalar_index` keep their `Unsupported` refusal (code 0, HTTP 406). The AST gate that looked for `native.call(...)` in source
is deleted: it could not see `run_in_threadpool(native.call, ...)`, which is how restore escaped it. In its place
`tests/integration/test_a_declared_branch_is_never_silently_dropped.py` drives each of the fourteen doors that hand a
branch-carrying spec model to the upstream op, with a branch the table lacks, through the real app on real pylance, and
pins each door's answer and that main took no commit. Accepted with it: a door added later is probed only once it is
added to that list; no structural check finds it.
