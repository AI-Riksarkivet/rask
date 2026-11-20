# 0221. Protection guards a table's drop, not its rows (2026-11-20)

**Status:** accepted. Source: XC-219 (#843), finding 5 #5 of the 2026-11-18 whole-lakehouse audit, decided by the owner
on 2026-11-20: "Drops only". Supersedes the protection clause of ADR 0099; the rest of ADR 0099 stands.

## Context

ADR 0099 made a protected table refuse both Overwrite modes (`create?mode=Overwrite`, `insert?mode=overwrite`, and a
stock client's `Overwrite` at `version/create`) 409 unless `force=true`, on the reasoning that an overwrite removes
every row as a drop does. The audit measured the other two doors that remove every row: `delete` with predicate `true`
and `merge_insert` with `when_not_matched_by_source_delete` each empty a protected table and ask nothing of its
protection. One rule was spelled two ways.

The two could not be made one by guarding more doors. Whether a predicate matches every row is not something the
catalog can judge without running it, so a guard on `delete` would either refuse every delete on a protected table,
one row included, or be passed by `id >= 0`. A guard that the next spelling passes is not a guard.

## Decision

Protection guards what ends a table: its drop, its deregister, its rename away and the cascade of a namespace that
holds it. It guards no write of the table's rows. A protected table takes an overwrite, a delete and a merge like any
other table, each judged by the caller's rung, the run rule and the table's data contract.

- `create?mode=Overwrite` over an existing table keeps its owner-tier `can_drop` gate, since it replaces the tip's
  schema as well as its rows, and asks nothing of protection.
- `insert?mode=overwrite` asks the writer rung, as before, and nothing of protection.
- `version/create` judges an `Overwrite` by `can_drop`, and reads no protection record.
- Neither Arrow door takes a `force` parameter.

## Why

- **Lance-idiomatic.** An overwrite, a delete and a merge each commit one more version of the same dataset, and every
  earlier version stays readable until version expiry removes it (`lance_docs/lance/guide/read_and_write.md`), and
  `RestoreTable` brings one back (`lance_docs/ns_catalog/spec.yaml`). A drop ends the table, which is what protection
  exists to stop.
- **Zero trust.** A rule that one spelling of an operation meets and another passes reads stronger than it is. What a
  caller may do to a table's rows is stated where it is enforced on every spelling: the grant.

## Consequences

A project that wants a table's rows held against a writer withholds the writer's grant, or declares the run rule on
its namespace. Keeping a protected table's earlier versions readable for longer is its maintenance policy's retention.
Pinned by `tests/integration/test_an_overwrite_is_a_new_version_of_the_same_table.py` at the two Arrow doors and by
`services/catalog/tests/test_a_stock_client_commits_every_operation_through_version_create.py` at `version/create`.
